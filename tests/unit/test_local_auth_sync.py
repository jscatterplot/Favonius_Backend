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
    _BOOTSTRAP_CONFIG_KEYS,
    BootstrapOutcome,
    SyncResult,
    _bootstrap_local_auth_config,
    _format_entries,
    _normalize_firmware,
    _probe_local_auth_support,
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
    firmware_version: str | None = None,
    get_configuration_response: Any | None = None,
    get_configuration_raises: Exception | None = None,
) -> MagicMock:
    """Build a fake FleetChargePoint with the methods sync_charger calls.

    ``get_configuration_response`` controls the probe path: passing
    ``{"configuration_key": [...]}`` simulates a wire reply; the default
    (``None`` → empty reply) simulates a charger that returns neither
    ``SupportedFeatureProfiles`` nor ``LocalAuthListMaxLength`` so the
    probe is ambiguous and sync falls through to ``send_local_list``.
    """
    cp = MagicMock()
    cp.vendor = vendor
    cp.firmware_version = firmware_version
    cp.change_configuration = AsyncMock(return_value="Accepted")
    if send_raises is not None:
        cp.send_local_list = AsyncMock(side_effect=send_raises)
    else:
        cp.send_local_list = AsyncMock(return_value=send_status)
    if get_configuration_raises is not None:
        cp.get_configuration = AsyncMock(side_effect=get_configuration_raises)
    elif get_configuration_response is not None:
        cp.get_configuration = AsyncMock(return_value=get_configuration_response)
    else:
        cp.get_configuration = AsyncMock(return_value={"configuration_key": [], "unknown_key": []})
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

        assert result == SyncResult(status="skipped", version=0, entries=0, reason="env_disabled")
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
    async def test_db_error_listing_tags_skipped_without_state_change(self, monkeypatch) -> None:
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
    async def test_first_sync_pushes_bootstrap_config_then_full_list(self, monkeypatch) -> None:
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
    async def test_subsequent_sync_skips_bootstrap_and_bumps_version(self, monkeypatch) -> None:
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
    async def test_not_supported_records_status_without_bumping_version(self, monkeypatch) -> None:
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
    async def test_send_local_list_raises_is_caught_as_failed(self, monkeypatch) -> None:
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
    async def test_db_error_during_state_update_is_swallowed(self, monkeypatch) -> None:
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
    """Direct unit tests for ``_bootstrap_local_auth_config``.

    The function gained a return value (``BootstrapOutcome``) and a
    fail-fast contract on the first critical key (``LocalAuthListEnabled``).
    The auxiliary keys remain best-effort. These tests pin both rules.
    """

    @pytest.mark.asyncio
    async def test_all_keys_accepted_returns_success(self) -> None:
        cp = MagicMock()
        cp.change_configuration = AsyncMock(return_value="Accepted")
        outcome = await _bootstrap_local_auth_config(cp, "station-001")
        assert outcome is BootstrapOutcome.SUCCESS
        # All four keys pushed when the critical key succeeds.
        assert cp.change_configuration.await_count == len(_BOOTSTRAP_CONFIG_KEYS)

    @pytest.mark.asyncio
    async def test_critical_key_notsupported_returns_unsupported_and_skips_rest(
        self,
    ) -> None:
        """LocalAuthListEnabled=NotSupported is the strongest signal that the
        firmware doesn't implement LocalAuthListManagement. Fail-fast: do NOT
        try the other three keys."""
        cp = MagicMock()
        cp.change_configuration = AsyncMock(return_value="NotSupported")
        outcome = await _bootstrap_local_auth_config(cp, "station-001")
        assert outcome is BootstrapOutcome.UNSUPPORTED
        # Only the critical key was attempted.
        assert cp.change_configuration.await_count == 1
        assert cp.change_configuration.await_args.args[0] == _BOOTSTRAP_CONFIG_KEYS[0][0]

    @pytest.mark.asyncio
    async def test_critical_key_rejected_returns_unsupported_and_skips_rest(self) -> None:
        cp = MagicMock()
        cp.change_configuration = AsyncMock(return_value="Rejected")
        outcome = await _bootstrap_local_auth_config(cp, "station-001")
        assert outcome is BootstrapOutcome.UNSUPPORTED
        assert cp.change_configuration.await_count == 1

    @pytest.mark.asyncio
    async def test_critical_key_raises_returns_unsupported_and_skips_rest(self) -> None:
        cp = MagicMock()
        cp.change_configuration = AsyncMock(side_effect=RuntimeError("boom"))
        outcome = await _bootstrap_local_auth_config(cp, "station-001")
        assert outcome is BootstrapOutcome.UNSUPPORTED
        assert cp.change_configuration.await_count == 1

    @pytest.mark.asyncio
    async def test_critical_key_timeout_returns_unsupported_and_skips_rest(self) -> None:
        """The HRX Vilnius failure mode: bootstrap hangs because the charger
        ACKed the WS at TCP level but never returned the ChangeConfiguration
        result. The per-call wait_for must catch this."""

        async def _hang(*_args, **_kwargs):
            await asyncio.sleep(60)  # would wedge the test if wait_for didn't fire

        cp = MagicMock()
        cp.change_configuration = AsyncMock(side_effect=_hang)

        # Patch the per-call timeout to a tiny value so the test is fast.
        from src.adapters.ocpp import local_auth_sync as module_under_test

        original_timeout = module_under_test._BOOTSTRAP_CHANGECONFIG_TIMEOUT_S
        module_under_test._BOOTSTRAP_CHANGECONFIG_TIMEOUT_S = 0.05
        try:
            outcome = await _bootstrap_local_auth_config(cp, "station-001")
        finally:
            module_under_test._BOOTSTRAP_CHANGECONFIG_TIMEOUT_S = original_timeout

        assert outcome is BootstrapOutcome.UNSUPPORTED
        assert cp.change_configuration.await_count == 1

    @pytest.mark.asyncio
    async def test_auxiliary_key_failure_does_not_block_success(self) -> None:
        """If the critical key succeeds and an auxiliary key fails, the
        bootstrap is still SUCCESS and SendLocalList proceeds. The aux key
        outcome is logged at WARN but does not gate the push — matches the
        pre-fix contract for non-critical keys."""
        # First call (critical): Accepted. Second call: NotSupported.
        # Third + fourth: Accepted.
        responses = ["Accepted", "NotSupported", "Accepted", "Accepted"]
        cp = MagicMock()
        cp.change_configuration = AsyncMock(side_effect=responses)
        outcome = await _bootstrap_local_auth_config(cp, "station-001")
        assert outcome is BootstrapOutcome.SUCCESS
        # All four keys still attempted because the critical one succeeded.
        assert cp.change_configuration.await_count == len(_BOOTSTRAP_CONFIG_KEYS)

    @pytest.mark.asyncio
    async def test_auxiliary_key_exception_does_not_block_success(self) -> None:
        """Critical succeeds; aux raises; outcome stays SUCCESS."""
        # Critical Accepted, then aux raises, then remaining keys accept.
        side_effects: list[Any] = ["Accepted", RuntimeError("aux failure"), "Accepted", "Accepted"]
        cp = MagicMock()
        cp.change_configuration = AsyncMock(side_effect=side_effects)
        outcome = await _bootstrap_local_auth_config(cp, "station-001")
        assert outcome is BootstrapOutcome.SUCCESS
        assert cp.change_configuration.await_count == len(_BOOTSTRAP_CONFIG_KEYS)

    @pytest.mark.asyncio
    async def test_reboot_required_on_critical_key_is_success(self) -> None:
        """``RebootRequired`` is OCPP 1.6's "Accepted, but apply on reboot"
        path; it must NOT be treated as Unsupported."""
        cp = MagicMock()
        cp.change_configuration = AsyncMock(return_value="RebootRequired")
        outcome = await _bootstrap_local_auth_config(cp, "station-001")
        assert outcome is BootstrapOutcome.SUCCESS

    @pytest.mark.asyncio
    async def test_cancelled_error_propagates(self) -> None:
        """If the WebSocket drops mid-bootstrap, the sync task is cancelled.
        We must propagate CancelledError, not swallow it."""
        cp = MagicMock()
        cp.change_configuration = AsyncMock(side_effect=asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await _bootstrap_local_auth_config(cp, "station-001")


class TestSyncChargerBootstrapUnsupportedSkipsSendLocalList:
    """L3 contract: when the bootstrap returns UNSUPPORTED, the caller MUST
    cache the firmware-scoped negative AND skip ``SendLocalList`` entirely.
    Without this branch a charger stuck in first-sync re-fires the whole
    sequence on every reconnect (HRX Vilnius ABB Terra AC V1.8.x)."""

    @pytest.mark.asyncio
    async def test_critical_key_notsupported_skips_send_and_writes_cache(
        self, monkeypatch
    ) -> None:
        monkeypatch.delenv("OCPP_DISABLE_LOCAL_AUTH_LIST", raising=False)
        db = _make_db(
            station_row={
                "id": "uuid-1",
                "local_list_version": 0,
                "local_list_supported": None,
                "local_list_probed_firmware": None,
            },
            id_tag_rows=[{"id_tag": "VEH-1", "source": "vehicle"}],
        )
        cp = _make_cp(
            firmware_version="V1.8.36",
            get_configuration_response={"configuration_key": [], "unknown_key": []},
        )
        cp.change_configuration = AsyncMock(return_value="NotSupported")

        result = await sync_charger(cp, db, "station-001")

        assert result.status == "UnsupportedFromBootstrap"
        assert result.reason == "bootstrap_unsupported"
        cp.send_local_list.assert_not_awaited()
        # Cache write recorded the firmware-scoped negative.
        update_call = db.execute.await_args_list[-1]
        assert "local_list_supported = $1" in update_call.args[0]
        assert update_call.args[1] is False
        assert update_call.args[2] == "V1.8.36"
        assert update_call.args[3] == "UnsupportedFromBootstrap"

    @pytest.mark.asyncio
    async def test_critical_key_raises_skips_send_and_writes_cache(self, monkeypatch) -> None:
        monkeypatch.delenv("OCPP_DISABLE_LOCAL_AUTH_LIST", raising=False)
        db = _make_db(
            station_row={
                "id": "uuid-1",
                "local_list_version": 0,
                "local_list_supported": None,
                "local_list_probed_firmware": None,
            },
            id_tag_rows=[{"id_tag": "VEH-1", "source": "vehicle"}],
        )
        cp = _make_cp(
            firmware_version="V1.8.36",
            get_configuration_response={"configuration_key": [], "unknown_key": []},
        )
        cp.change_configuration = AsyncMock(side_effect=RuntimeError("boom"))

        result = await sync_charger(cp, db, "station-001")

        assert result.status == "UnsupportedFromBootstrap"
        cp.send_local_list.assert_not_awaited()
        update_call = db.execute.await_args_list[-1]
        assert update_call.args[1] is False
        assert update_call.args[3] == "UnsupportedFromBootstrap"

    @pytest.mark.asyncio
    async def test_bootstrap_success_still_calls_send_local_list(self, monkeypatch) -> None:
        """Sanity: the happy path is unchanged. If the critical key is
        accepted, SendLocalList runs and the version bumps."""
        monkeypatch.delenv("OCPP_DISABLE_LOCAL_AUTH_LIST", raising=False)
        db = _make_db(
            station_row={
                "id": "uuid-1",
                "local_list_version": 0,
                "local_list_supported": None,
                "local_list_probed_firmware": None,
            },
            id_tag_rows=[{"id_tag": "VEH-1", "source": "vehicle"}],
        )
        cp = _make_cp(
            firmware_version="V1.8.36",
            get_configuration_response={"configuration_key": [], "unknown_key": []},
            send_status="Accepted",
        )
        # change_configuration default in fixture is "Accepted" for all keys.

        result = await sync_charger(cp, db, "station-001")

        assert result.status == "Accepted"
        cp.send_local_list.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_bootstrap_unsupported_under_legacy_schema_stamps_last_status_only(
        self, monkeypatch
    ) -> None:
        """When migration 012 isn't applied (legacy schema), we cannot write
        the probe cache. We must still record ``UnsupportedFromBootstrap``
        in ``local_list_last_status`` so ops can see what happened, AND
        still skip SendLocalList."""
        monkeypatch.delenv("OCPP_DISABLE_LOCAL_AUTH_LIST", raising=False)

        # Simulate the legacy-schema fallback: first SELECT raises 42703.
        first_call = _UndefinedColumnError()
        legacy_row = {"id": "uuid-1", "local_list_version": 0}
        fetchrow_mock = AsyncMock(side_effect=[first_call, legacy_row])
        db = MagicMock()
        db.fetchrow = fetchrow_mock
        db.fetch = AsyncMock(return_value=[{"id_tag": "VEH-1", "source": "vehicle"}])
        db.execute = AsyncMock()

        cp = _make_cp(firmware_version="V1.8.36")
        cp.change_configuration = AsyncMock(return_value="NotSupported")

        result = await sync_charger(cp, db, "station-001")

        assert result.status == "UnsupportedFromBootstrap"
        cp.send_local_list.assert_not_awaited()
        # Last UPDATE is the legacy "stamp last_status only" variant.
        update_call = db.execute.await_args_list[-1]
        assert "local_list_supported" not in update_call.args[0]
        assert "local_list_last_status" in update_call.args[0]
        assert update_call.args[1] == "UnsupportedFromBootstrap"


class TestSyncChargerFailedFallbackCache:
    """L4 contract: ``SendLocalList: Failed`` is cached as firmware-permanent
    only when we do NOT have prior positive evidence of support. This
    protects supported chargers from being permanently disabled after a
    transient internal error, while still catching the ABB-Terra-AC-like
    cases where ``Failed`` is the real "your firmware doesn't do this"
    response."""

    @pytest.mark.asyncio
    async def test_failed_after_ambiguous_probe_caches_negative(self, monkeypatch) -> None:
        """Probe returns no useful keys → ambiguous. SendLocalList returns
        Failed. Cache supported=False so the next reconnect short-circuits."""
        monkeypatch.delenv("OCPP_DISABLE_LOCAL_AUTH_LIST", raising=False)
        db = _make_db(
            station_row={
                "id": "uuid-1",
                "local_list_version": 0,
                "local_list_supported": None,
                "local_list_probed_firmware": None,
            },
            id_tag_rows=[{"id_tag": f"VEH-{i}", "source": "vehicle"} for i in range(10)],
        )
        cp = _make_cp(
            firmware_version="V1.8.36",
            get_configuration_response={"configuration_key": [], "unknown_key": []},
            send_status="Failed",
        )

        result = await sync_charger(cp, db, "station-001")

        assert result.status == "Failed"
        # Probe-cache write happened (firmware-scoped negative).
        update_call = db.execute.await_args_list[-1]
        assert "local_list_supported = $1" in update_call.args[0]
        assert update_call.args[1] is False
        assert update_call.args[3] == "Failed"

    @pytest.mark.asyncio
    async def test_failed_after_positive_probe_does_not_cache_negative(
        self, monkeypatch
    ) -> None:
        """If we already have positive evidence of support (probe returned
        True this turn), a Failed is treated as transient — last_status
        only, no firmware-permanent cache."""
        monkeypatch.delenv("OCPP_DISABLE_LOCAL_AUTH_LIST", raising=False)
        db = _make_db(
            station_row={
                "id": "uuid-1",
                "local_list_version": 0,
                "local_list_supported": None,
                "local_list_probed_firmware": None,
            },
            id_tag_rows=[{"id_tag": "VEH-1", "source": "vehicle"}],
        )
        cp = _make_cp(
            firmware_version="V1.9.0",
            get_configuration_response={
                "configuration_key": [
                    {
                        "key": "SupportedFeatureProfiles",
                        "value": "Core,LocalAuthListManagement",
                    },
                ],
                "unknown_key": [],
            },
            send_status="Failed",
        )

        result = await sync_charger(cp, db, "station-001")

        assert result.status == "Failed"
        # Last UPDATE is the "stamp last_status only" path — no
        # local_list_supported flip.
        update_call = db.execute.await_args_list[-1]
        assert "local_list_supported" not in update_call.args[0]
        assert "local_list_last_status" in update_call.args[0]
        assert update_call.args[1] == "Failed"

    @pytest.mark.asyncio
    async def test_failed_after_cached_positive_does_not_cache_negative(
        self, monkeypatch
    ) -> None:
        """Same protection on subsequent reconnects: if the cache already
        says supported=True for this firmware, a Failed stays transient."""
        monkeypatch.delenv("OCPP_DISABLE_LOCAL_AUTH_LIST", raising=False)
        db = _make_db(
            station_row={
                "id": "uuid-1",
                "local_list_version": 5,
                "local_list_supported": True,
                "local_list_probed_firmware": "V1.9.0",
            },
            id_tag_rows=[{"id_tag": "VEH-1", "source": "vehicle"}],
        )
        cp = _make_cp(firmware_version="V1.9.0", send_status="Failed")

        result = await sync_charger(cp, db, "station-001")

        assert result.status == "Failed"
        update_call = db.execute.await_args_list[-1]
        assert "local_list_supported" not in update_call.args[0]
        assert update_call.args[1] == "Failed"

    @pytest.mark.asyncio
    async def test_failed_over_cap_does_not_cache_negative(self, monkeypatch) -> None:
        """Our local 16-cap refusal is also returned as ``Failed`` semantics
        via ``send_local_list``. Caching over-cap as firmware-permanent
        would be wrong — same protection as the existing NotSupported
        over-cap case."""
        monkeypatch.delenv("OCPP_DISABLE_LOCAL_AUTH_LIST", raising=False)
        db = _make_db(
            station_row={
                "id": "uuid-1",
                "local_list_version": 0,
                "local_list_supported": None,
                "local_list_probed_firmware": None,
            },
            id_tag_rows=[{"id_tag": f"VEH-{i}", "source": "vehicle"} for i in range(17)],
        )
        cp = _make_cp(
            firmware_version="V1.8.36",
            get_configuration_response={"configuration_key": [], "unknown_key": []},
            send_status="Failed",
        )

        result = await sync_charger(cp, db, "station-001")

        assert result.status == "Failed"
        update_call = db.execute.await_args_list[-1]
        assert "local_list_supported" not in update_call.args[0]
        assert update_call.args[1] == "Failed"


class TestNormalizeFirmware:
    """L5 contract: firmware strings are compared trimmed so cache misses
    don't churn on whitespace-only differences."""

    def test_none_returns_none(self) -> None:
        assert _normalize_firmware(None) is None

    def test_strips_surrounding_whitespace(self) -> None:
        assert _normalize_firmware("  V1.8.36 ") == "V1.8.36"

    def test_no_change_for_already_clean_string(self) -> None:
        assert _normalize_firmware("V1.8.36") == "V1.8.36"

    @pytest.mark.asyncio
    async def test_cache_hit_when_firmware_has_extra_whitespace(self, monkeypatch) -> None:
        """A charger that reports trailing whitespace in firmware on this
        reconnect, when the previous probe stored the trimmed value, must
        still hit the cache and short-circuit — no extra probe / bootstrap."""
        monkeypatch.delenv("OCPP_DISABLE_LOCAL_AUTH_LIST", raising=False)
        db = _make_db(
            station_row={
                "id": "uuid-1",
                "local_list_version": 0,
                "local_list_supported": False,
                "local_list_probed_firmware": "V1.8.36",
            },
            id_tag_rows=[],
        )
        cp = _make_cp(firmware_version=" V1.8.36 ")

        result = await sync_charger(cp, db, "station-001")

        assert result.status == "skipped"
        assert result.reason == "unsupported_cached"
        cp.change_configuration.assert_not_awaited()
        cp.send_local_list.assert_not_awaited()
        cp.get_configuration.assert_not_awaited()


# ---------------------------------------------------------------------------
# _probe_local_auth_support
# ---------------------------------------------------------------------------


class TestProbeLocalAuthSupport:
    @pytest.mark.asyncio
    async def test_profiles_advertises_local_auth_list_management_returns_true(
        self,
    ) -> None:
        cp = _make_cp(
            get_configuration_response={
                "configuration_key": [
                    {
                        "key": "SupportedFeatureProfiles",
                        "value": "Core,FirmwareManagement,LocalAuthListManagement,SmartCharging",
                    },
                ],
                "unknown_key": [],
            }
        )
        result = await _probe_local_auth_support(cp, "station-001")
        assert result is True

    @pytest.mark.asyncio
    async def test_profiles_missing_local_auth_list_returns_false(self) -> None:
        """The HRX/ABB Terra AC V1.8.x case: profile is omitted."""
        cp = _make_cp(
            get_configuration_response={
                "configuration_key": [
                    {
                        "key": "SupportedFeatureProfiles",
                        "value": "Core,FirmwareManagement,SmartCharging",
                    },
                ],
                "unknown_key": [],
            }
        )
        result = await _probe_local_auth_support(cp, "station-002")
        assert result is False

    @pytest.mark.asyncio
    async def test_profile_match_is_case_and_whitespace_insensitive(self) -> None:
        cp = _make_cp(
            get_configuration_response={
                "configuration_key": [
                    {
                        "key": "supportedfeatureprofiles",  # lowercase key
                        "value": " Core , LOCALAUTHLISTMANAGEMENT , SmartCharging ",
                    },
                ],
                "unknown_key": [],
            }
        )
        assert await _probe_local_auth_support(cp, "station-003") is True

    @pytest.mark.asyncio
    async def test_neither_key_returned_is_ambiguous(self) -> None:
        cp = _make_cp(get_configuration_response={"configuration_key": [], "unknown_key": []})
        assert await _probe_local_auth_support(cp, "station-004") is None

    @pytest.mark.asyncio
    async def test_get_configuration_exception_is_ambiguous(self) -> None:
        cp = _make_cp(get_configuration_raises=RuntimeError("socket closed"))
        assert await _probe_local_auth_support(cp, "station-005") is None

    @pytest.mark.asyncio
    async def test_max_length_positive_implies_supported(self) -> None:
        """Some chargers omit SupportedFeatureProfiles but still expose the
        cap. A positive cap is a strong corroborating signal."""
        cp = _make_cp(
            get_configuration_response={
                "configuration_key": [
                    {"key": "LocalAuthListMaxLength", "value": "16"},
                ],
                "unknown_key": [],
            }
        )
        assert await _probe_local_auth_support(cp, "station-006") is True

    @pytest.mark.asyncio
    async def test_max_length_zero_implies_unsupported(self) -> None:
        cp = _make_cp(
            get_configuration_response={
                "configuration_key": [
                    {"key": "LocalAuthListMaxLength", "value": "0"},
                ],
                "unknown_key": [],
            }
        )
        assert await _probe_local_auth_support(cp, "station-007") is False

    @pytest.mark.asyncio
    async def test_max_length_unparseable_is_ambiguous(self) -> None:
        cp = _make_cp(
            get_configuration_response={
                "configuration_key": [
                    {"key": "LocalAuthListMaxLength", "value": "n/a"},
                ],
                "unknown_key": [],
            }
        )
        assert await _probe_local_auth_support(cp, "station-008") is None


# ---------------------------------------------------------------------------
# sync_charger — probe-cache short-circuit + re-probe on firmware change
# ---------------------------------------------------------------------------


class TestSyncChargerProbeCache:
    @pytest.mark.asyncio
    async def test_cached_unsupported_same_firmware_short_circuits(self, monkeypatch) -> None:
        """The fix: ABB Terra AC V1.8.36 already known-unsupported on a previous
        boot must NOT re-attempt the probe or SendLocalList on reconnect."""
        monkeypatch.delenv("OCPP_DISABLE_LOCAL_AUTH_LIST", raising=False)
        db = _make_db(
            station_row={
                "id": "uuid-1",
                "local_list_version": 0,
                "local_list_supported": False,
                "local_list_probed_firmware": "TAC3Z9119006710273::V1.8.36",
            },
        )
        cp = _make_cp(firmware_version="TAC3Z9119006710273::V1.8.36")

        result = await sync_charger(cp, db, "station-001")

        assert result == SyncResult(
            status="skipped",
            version=0,
            entries=0,
            reason="unsupported_cached",
        )
        cp.get_configuration.assert_not_awaited()
        cp.send_local_list.assert_not_awaited()
        cp.change_configuration.assert_not_awaited()
        db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cached_unsupported_different_firmware_reprobes(self, monkeypatch) -> None:
        """Firmware upgrade clears the cached negative — we must probe again
        because the new firmware may have added support."""
        monkeypatch.delenv("OCPP_DISABLE_LOCAL_AUTH_LIST", raising=False)
        db = _make_db(
            station_row={
                "id": "uuid-1",
                "local_list_version": 0,
                "local_list_supported": False,
                "local_list_probed_firmware": "V1.8.36",
            },
            id_tag_rows=[{"id_tag": "VEH-1", "source": "vehicle"}],
        )
        cp = _make_cp(
            firmware_version="V2.0.0",  # different firmware
            get_configuration_response={
                "configuration_key": [
                    {
                        "key": "SupportedFeatureProfiles",
                        "value": "Core,LocalAuthListManagement",
                    },
                ],
                "unknown_key": [],
            },
            send_status="Accepted",
        )

        result = await sync_charger(cp, db, "station-001")

        cp.get_configuration.assert_awaited_once()
        cp.send_local_list.assert_awaited_once()
        assert result.status == "Accepted"
        assert result.version == 1

    @pytest.mark.asyncio
    async def test_probe_negative_records_outcome_and_skips_send(self, monkeypatch) -> None:
        """First-time probe returns False → record FALSE + firmware + status,
        return UnsupportedFeatureProfile, never call SendLocalList."""
        monkeypatch.delenv("OCPP_DISABLE_LOCAL_AUTH_LIST", raising=False)
        db = _make_db(
            station_row={
                "id": "uuid-1",
                "local_list_version": 0,
                "local_list_supported": None,
                "local_list_probed_firmware": None,
            },
        )
        cp = _make_cp(
            firmware_version="V1.8.36",
            get_configuration_response={
                "configuration_key": [
                    {
                        "key": "SupportedFeatureProfiles",
                        "value": "Core,FirmwareManagement,SmartCharging",
                    },
                ],
                "unknown_key": [],
            },
        )

        result = await sync_charger(cp, db, "station-001")

        assert result.status == "UnsupportedFeatureProfile"
        assert result.reason == "probe_negative"
        assert result.version == 0
        assert result.entries == 0
        cp.send_local_list.assert_not_awaited()
        cp.change_configuration.assert_not_awaited()

        # DB UPDATE wrote the firmware-scoped negative.
        update_call = db.execute.await_args_list[-1]
        assert "local_list_supported = $1" in update_call.args[0]
        assert "local_list_probed_firmware = $2" in update_call.args[0]
        assert update_call.args[1] is False
        assert update_call.args[2] == "V1.8.36"
        assert update_call.args[3] == "UnsupportedFeatureProfile"

    @pytest.mark.asyncio
    async def test_probe_positive_records_outcome_and_proceeds(self, monkeypatch) -> None:
        monkeypatch.delenv("OCPP_DISABLE_LOCAL_AUTH_LIST", raising=False)
        db = _make_db(
            station_row={
                "id": "uuid-1",
                "local_list_version": 0,
                "local_list_supported": None,
                "local_list_probed_firmware": None,
            },
            id_tag_rows=[{"id_tag": "VEH-1", "source": "vehicle"}],
        )
        cp = _make_cp(
            firmware_version="V2.0.0",
            get_configuration_response={
                "configuration_key": [
                    {
                        "key": "SupportedFeatureProfiles",
                        "value": "Core,LocalAuthListManagement",
                    },
                ],
                "unknown_key": [],
            },
            send_status="Accepted",
        )

        result = await sync_charger(cp, db, "station-001")

        assert result.status == "Accepted"
        cp.send_local_list.assert_awaited_once()
        # First UPDATE recorded the positive probe (supported=True, firmware,
        # no last_status); second UPDATE bumped version after Accepted push.
        update_calls = db.execute.await_args_list
        assert len(update_calls) == 2
        probe_update = update_calls[0]
        assert "local_list_supported = $1" in probe_update.args[0]
        assert probe_update.args[1] is True
        assert probe_update.args[2] == "V2.0.0"

    @pytest.mark.asyncio
    async def test_probe_ambiguous_falls_through_to_send(self, monkeypatch) -> None:
        """Charger doesn't expose the keys — we still try SendLocalList; this
        preserves the pre-probe behavior for non-standard firmware."""
        monkeypatch.delenv("OCPP_DISABLE_LOCAL_AUTH_LIST", raising=False)
        db = _make_db(
            station_row={
                "id": "uuid-1",
                "local_list_version": 0,
                "local_list_supported": None,
                "local_list_probed_firmware": None,
            },
            id_tag_rows=[{"id_tag": "VEH-1", "source": "vehicle"}],
        )
        cp = _make_cp(
            firmware_version="V1.0.0",
            get_configuration_response={"configuration_key": [], "unknown_key": []},
            send_status="Accepted",
        )

        result = await sync_charger(cp, db, "station-001")

        assert result.status == "Accepted"
        cp.get_configuration.assert_awaited_once()
        cp.send_local_list.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cached_supported_skips_probe_but_still_pushes(self, monkeypatch) -> None:
        """A previously confirmed-supported charger doesn't need to re-probe
        on every reconnect — we go straight to the push."""
        monkeypatch.delenv("OCPP_DISABLE_LOCAL_AUTH_LIST", raising=False)
        db = _make_db(
            station_row={
                "id": "uuid-1",
                "local_list_version": 3,
                "local_list_supported": True,
                "local_list_probed_firmware": "V1.0.0",
            },
            id_tag_rows=[{"id_tag": "VEH-1", "source": "vehicle"}],
        )
        cp = _make_cp(firmware_version="V1.0.0", send_status="Accepted")

        result = await sync_charger(cp, db, "station-001")

        assert result.status == "Accepted"
        assert result.version == 4
        cp.get_configuration.assert_not_awaited()
        cp.send_local_list.assert_awaited_once()


# ---------------------------------------------------------------------------
# sync_charger — fallback caching when SendLocalList itself returns NotSupported
# ---------------------------------------------------------------------------


class TestSyncChargerNotSupportedFallbackCache:
    @pytest.mark.asyncio
    async def test_charger_not_supported_under_cap_caches_firmware_negative(
        self, monkeypatch
    ) -> None:
        """If the probe was ambiguous and SendLocalList returned NotSupported
        with entry count under the 16-cap, we know the charger itself said no
        — cache the firmware-scoped negative so the next boot skips."""
        monkeypatch.delenv("OCPP_DISABLE_LOCAL_AUTH_LIST", raising=False)
        db = _make_db(
            station_row={
                "id": "uuid-1",
                "local_list_version": 0,
                "local_list_supported": None,
                "local_list_probed_firmware": None,
            },
            id_tag_rows=[{"id_tag": f"VEH-{i}", "source": "vehicle"} for i in range(15)],
        )
        cp = _make_cp(
            firmware_version="V1.8.36",
            get_configuration_response={"configuration_key": [], "unknown_key": []},
            send_status="NotSupported",
        )

        result = await sync_charger(cp, db, "station-001")

        assert result.status == "NotSupported"
        # Last UPDATE recorded firmware-scoped negative (not just last_status).
        update_call = db.execute.await_args_list[-1]
        assert "local_list_supported = $1" in update_call.args[0]
        assert update_call.args[1] is False
        assert update_call.args[2] == "V1.8.36"
        assert update_call.args[3] == "NotSupported"

    @pytest.mark.asyncio
    async def test_local_cap_refusal_over_cap_does_not_cache_firmware_negative(
        self, monkeypatch
    ) -> None:
        """When the entry count exceeds the ABB 16-cap, FleetChargePoint
        refuses LOCALLY and returns NotSupported. That's transient — adding
        a tag from a different driver would not make it firmware-permanent.
        We must record last_status only, NOT supported=False."""
        monkeypatch.delenv("OCPP_DISABLE_LOCAL_AUTH_LIST", raising=False)
        db = _make_db(
            station_row={
                "id": "uuid-1",
                "local_list_version": 0,
                "local_list_supported": None,
                "local_list_probed_firmware": None,
            },
            id_tag_rows=[{"id_tag": f"VEH-{i}", "source": "vehicle"} for i in range(17)],
        )
        cp = _make_cp(
            firmware_version="V1.8.36",
            get_configuration_response={"configuration_key": [], "unknown_key": []},
            send_status="NotSupported",
        )

        result = await sync_charger(cp, db, "station-001")

        assert result.status == "NotSupported"
        # Last UPDATE is the "stamp last_status only" variant — does NOT
        # touch local_list_supported.
        update_call = db.execute.await_args_list[-1]
        assert "local_list_supported" not in update_call.args[0]
        assert "local_list_last_status" in update_call.args[0]
        assert update_call.args[1] == "NotSupported"


# ---------------------------------------------------------------------------
# sync_charger — legacy schema fallback when migration 012 hasn't applied
# ---------------------------------------------------------------------------


class _UndefinedColumnError(Exception):
    """Stand-in for asyncpg.exceptions.UndefinedColumnError.

    asyncpg surfaces Postgres SQLSTATE on the exception instance; the
    production code keys off ``sqlstate == "42703"`` rather than the class
    name so this test fixture stays driver-agnostic.
    """

    sqlstate = "42703"


class TestSyncChargerLegacySchemaFallback:
    """Regression: chargers entering connect-loop when migration 012 hasn't
    been applied to Supabase.

    PR #167 widened the ``charging_stations`` SELECT to include
    ``local_list_supported`` / ``local_list_probed_firmware``. On a DB that
    hasn't run ``migrations/supabase/012_local_list_support_probe.sql``,
    asyncpg raises ``UndefinedColumnError`` (sqlstate 42703) and the old
    code path silently aborted — leaving ABB Terra AC 1.8.x with zero
    post-boot OCPP traffic from the server, which the firmware treats as a
    half-open session and reconnects ~60 s later. These tests pin the
    fallback: legacy SELECT shape, skip probe + cache UPDATEs, still
    deliver the bootstrap config + SendLocalList exchange that ABB needs.
    """

    @pytest.mark.asyncio
    async def test_falls_back_and_sends_local_list_when_probe_columns_missing(
        self, monkeypatch
    ) -> None:
        monkeypatch.delenv("OCPP_DISABLE_LOCAL_AUTH_LIST", raising=False)
        db = MagicMock()
        # First fetchrow (new shape) raises UndefinedColumnError; retry with
        # legacy shape returns a minimal row.
        db.fetchrow = AsyncMock(
            side_effect=[
                _UndefinedColumnError(
                    'column "local_list_supported" does not exist'
                ),
                {"id": "uuid-1", "local_list_version": 0},
            ]
        )
        db.fetch = AsyncMock(return_value=[{"id_tag": "VEH-1", "source": "vehicle"}])
        db.execute = AsyncMock()

        cp = _make_cp(firmware_version="TAC3Z9119006710273::V1.8.36")

        result = await sync_charger(cp, db, "station-001")

        # Retry happened with the legacy SELECT shape.
        assert db.fetchrow.await_count == 2
        legacy_select = db.fetchrow.await_args_list[1].args[0]
        assert "local_list_supported" not in legacy_select
        assert "local_list_probed_firmware" not in legacy_select
        assert "local_list_version" in legacy_select

        # Probe is suppressed under legacy schema — the round-trip would have
        # nowhere to cache its outcome.
        cp.get_configuration.assert_not_awaited()

        # First sync still pushes bootstrap config + the full list, matching
        # pre-PR-#167 behaviour that kept ABB chargers from looping.
        assert cp.change_configuration.await_count == len(_BOOTSTRAP_CONFIG_KEYS)
        cp.send_local_list.assert_awaited_once()

        # Result reflects the actual SendLocalList response.
        assert result.status == "Accepted"
        assert result.version == 1

        # The success UPDATE only touches columns that exist via migration 011.
        update_sql = db.execute.await_args_list[-1].args[0]
        assert "local_list_version = $1" in update_sql
        assert "local_list_supported" not in update_sql
        assert "local_list_probed_firmware" not in update_sql

    @pytest.mark.asyncio
    async def test_legacy_fallback_skips_probe_outcome_update_on_not_supported(
        self, monkeypatch
    ) -> None:
        """ABB on legacy DB returns NotSupported from SendLocalList. We must
        NOT try to UPDATE the missing ``local_list_supported`` column —
        we'd just raise inside the existing db_error handler. Instead the
        ``local_list_last_status`` UPDATE (migration 011) runs."""
        monkeypatch.delenv("OCPP_DISABLE_LOCAL_AUTH_LIST", raising=False)
        db = MagicMock()
        db.fetchrow = AsyncMock(
            side_effect=[
                _UndefinedColumnError(
                    'column "local_list_supported" does not exist'
                ),
                {"id": "uuid-1", "local_list_version": 0},
            ]
        )
        db.fetch = AsyncMock(return_value=[{"id_tag": "VEH-1", "source": "vehicle"}])
        db.execute = AsyncMock()

        cp = _make_cp(
            firmware_version="TAC3Z9119006710273::V1.8.36",
            send_status="NotSupported",
        )

        result = await sync_charger(cp, db, "station-001")

        assert result.status == "NotSupported"
        update_sql = db.execute.await_args_list[-1].args[0]
        assert "local_list_supported" not in update_sql
        assert "local_list_last_status" in update_sql
        assert db.execute.await_args_list[-1].args[1] == "NotSupported"

    @pytest.mark.asyncio
    async def test_non_42703_db_error_still_aborts(self, monkeypatch) -> None:
        """Any error that is NOT a missing-column failure (connection reset,
        permission denied, etc.) must short-circuit like before — we only
        widen the tolerance for sqlstate 42703."""
        monkeypatch.delenv("OCPP_DISABLE_LOCAL_AUTH_LIST", raising=False)

        class _ConnectionFailure(Exception):
            sqlstate = "08006"  # connection_failure

        db = MagicMock()
        db.fetchrow = AsyncMock(side_effect=_ConnectionFailure("server gone"))
        db.fetch = AsyncMock(return_value=[])
        db.execute = AsyncMock()

        cp = _make_cp(firmware_version="V1.8.36")

        result = await sync_charger(cp, db, "station-001")

        assert result.status == "skipped"
        assert result.reason == "db_error"
        # No retry — the original guard still aborts immediately.
        assert db.fetchrow.await_count == 1
        cp.send_local_list.assert_not_awaited()


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
    async def test_on_boot_schedules_local_auth_sync_task(self, _session_factory) -> None:
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
        # The new static-pool fallback consults ``_static_pool()`` first; on a
        # bare MagicMock that returns a child MagicMock (i.e. NOT None), so
        # the skip branch would not fire without this stub.
        session._timescale._static_pool = MagicMock(return_value=None)
        # Pre-empt the replay sleep so the helper proceeds immediately.
        session._replay_task = None
        # Spy on sync_charger to ensure it is NOT called when pool is None.
        called = {"count": 0}

        async def _fake_sync(
            *args: Any, **kwargs: Any
        ) -> SyncResult:  # pragma: no cover - asserted via counter
            called["count"] += 1
            return SyncResult(status="Accepted", version=1, entries=0)

        monkeypatch.setattr("src.websocket_handler.ocpp16_adapter.sync_local_auth_list", _fake_sync)
        await session._delayed_local_auth_sync()
        assert called["count"] == 0

    @pytest.mark.asyncio
    async def test_delayed_local_auth_sync_uses_static_pool_when_pg_pool_none(
        self, _session_factory, monkeypatch
    ) -> None:
        """Split topology: static identities use Supabase pool; TS pool may differ."""
        session = _session_factory(pg_pool=None)
        static_pool = MagicMock()
        session._timescale._static_pool = MagicMock(return_value=static_pool)
        session._replay_task = None
        used: dict[str, Any] = {}

        async def _fake_sync(cp: Any, pool: Any, sid: str) -> SyncResult:
            used["pool"] = pool
            return SyncResult(status="skipped", version=0, entries=0)

        monkeypatch.setattr("src.websocket_handler.ocpp16_adapter.sync_local_auth_list", _fake_sync)
        await session._delayed_local_auth_sync()
        assert used["pool"] is static_pool

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
