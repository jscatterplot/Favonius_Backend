"""OCPP 1.6 protocol-correctness tests for the Monday pilot.

Covers the fixes from session 1 of the pilot-hardening plan:

  * BootNotification + Heartbeat respond with UTC ISO-8601 ms+Z timestamps.
  * StartTransaction draws transactionId from the injected provider (DB
    sequence in production), falling back to the in-memory counter only
    when the provider raises.
  * DataTransfer returns ``UnknownVendorId`` for vendors outside the
    allowlist.
  * ChangeConfiguration refuses to push ``MeterValuesSampledData`` /
    ``MeterValuesAlignedData`` with measurands outside the ABB-safe set.
  * SendLocalList caps at 16 entries for ABB chargers and returns
    ``NotSupported`` past that, so callers fall back to central authorization.
  * OCPP16Session.``_on_authorize`` returns ``Accepted`` for known
    ``vehicles.id_tag`` rows and ``Invalid`` otherwise.

Reference: /root/.claude/plans/hmm-but-i-want-floating-rabin.md
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock

import pytest
from ocpp.v16.enums import AuthorizationStatus

from src.adapters.ocpp.charge_point import (
    FleetChargePoint,
    _ABB_SAFE_MEASURANDS,
    _KNOWN_VENDORS,
    _LOCAL_LIST_MAX_ENTRIES,
    _now_iso_z,
    _requires_abb_safe_measurands,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_websocket() -> MagicMock:
    ws = MagicMock()
    ws.send = AsyncMock()
    ws.recv = AsyncMock()
    ws.close = AsyncMock()
    return ws


@pytest.fixture()
def cp(mock_websocket) -> FleetChargePoint:
    return FleetChargePoint(id="PILOT-01", connection=mock_websocket)


# ---------------------------------------------------------------------------
# _now_iso_z helper
# ---------------------------------------------------------------------------


class TestNowIsoZ:
    """OCPP 1.6 currentTime must be UTC, millisecond precision, trailing Z."""

    _PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")

    def test_format_matches_spec(self) -> None:
        ts = _now_iso_z()
        assert self._PATTERN.match(ts), ts

    def test_no_timezone_offset_other_than_z(self) -> None:
        ts = _now_iso_z()
        assert ts.endswith("Z")
        assert "+" not in ts
        assert "-" not in ts.split("T", 1)[1]


# ---------------------------------------------------------------------------
# BootNotification + Heartbeat time format
# ---------------------------------------------------------------------------


class TestBootHeartbeatTimeFormat:
    @pytest.mark.asyncio
    async def test_boot_notification_currenttime_has_z_and_ms(self, cp) -> None:
        result = await cp.on_boot_notification(
            charge_point_vendor="ABB",
            charge_point_model="Terra AC W11",
            charge_point_serial_number="ABB-001",
            firmware_version="1.8.21",
        )
        assert result.current_time.endswith("Z")
        assert re.match(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$",
            result.current_time,
        ), result.current_time
        assert result.interval >= 60
        assert result.status == "Accepted"

    @pytest.mark.asyncio
    async def test_heartbeat_currenttime_has_z_and_ms(self, cp) -> None:
        result = await cp.on_heartbeat()
        assert result.current_time.endswith("Z")
        assert re.match(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$",
            result.current_time,
        ), result.current_time


# ---------------------------------------------------------------------------
# StartTransaction tx_id provider
# ---------------------------------------------------------------------------


class TestStartTransactionProvider:
    @pytest.mark.asyncio
    async def test_uses_provider_when_set(self, mock_websocket) -> None:
        provider = AsyncMock(return_value=4242)
        cp = FleetChargePoint(
            id="PILOT-01",
            connection=mock_websocket,
            tx_id_provider=provider,
        )
        result = await cp.on_start_transaction(
            connector_id=1,
            id_tag="DEADBEEF01",
            meter_start=0,
            timestamp="2026-04-26T12:00:00.000Z",
        )
        provider.assert_awaited_once()
        assert result.transaction_id == 4242
        assert cp.transactions[1] == 4242

    @pytest.mark.asyncio
    async def test_falls_back_to_in_memory_when_provider_raises(
        self, mock_websocket, caplog
    ) -> None:
        provider = AsyncMock(side_effect=RuntimeError("DB down"))
        cp = FleetChargePoint(
            id="PILOT-01",
            connection=mock_websocket,
            tx_id_provider=provider,
        )
        result = await cp.on_start_transaction(
            connector_id=1,
            id_tag="DEADBEEF01",
            meter_start=0,
            timestamp="2026-04-26T12:00:00.000Z",
        )
        provider.assert_awaited_once()
        assert isinstance(result.transaction_id, int)
        assert result.transaction_id > 0
        assert "tx_id_provider failed" in caplog.text

    @pytest.mark.asyncio
    async def test_no_provider_uses_in_memory_counter(self, cp) -> None:
        result = await cp.on_start_transaction(
            connector_id=1,
            id_tag="DEADBEEF01",
            meter_start=0,
            timestamp="2026-04-26T12:00:00.000Z",
        )
        assert isinstance(result.transaction_id, int)
        assert result.transaction_id > 0


# ---------------------------------------------------------------------------
# DataTransfer allowlist
# ---------------------------------------------------------------------------


class TestDataTransferAllowlist:
    @pytest.mark.asyncio
    async def test_known_vendor_accepted(self, cp) -> None:
        # Sanity: pilot vendors must be in the allowlist.
        assert "ABB" in _KNOWN_VENDORS
        assert "Etrel" in _KNOWN_VENDORS

        result = await cp.on_data_transfer_request(
            vendor_id="ABB",
            message_id="diag-123",
            data='{"op":"ping"}',
        )
        assert result.status == "Accepted"

    @pytest.mark.asyncio
    async def test_unknown_vendor_returns_unknown_vendor_id(self, cp) -> None:
        result = await cp.on_data_transfer_request(
            vendor_id="RandomCo",
            message_id="x",
            data="payload",
        )
        assert result.status == "UnknownVendorId"

    @pytest.mark.asyncio
    async def test_vendor_variants_are_accepted(self, cp) -> None:
        for vendor in ("abb", "ABB Inc", "abb-terra", "Favonius Energy"):
            result = await cp.on_data_transfer_request(
                vendor_id=vendor,
                message_id="diag",
                data="payload",
            )
            assert result.status == "Accepted"

    @pytest.mark.asyncio
    async def test_callback_can_override_status(self, mock_websocket) -> None:
        cb = AsyncMock(return_value=("Rejected", None))
        cp = FleetChargePoint(
            id="PILOT-01",
            connection=mock_websocket,
            on_data_transfer=cb,
        )
        result = await cp.on_data_transfer_request(
            vendor_id="ABB",
            message_id="x",
            data="y",
        )
        cb.assert_awaited_once()
        assert result.status == "Rejected"


# ---------------------------------------------------------------------------
# ChangeConfiguration ABB measurand guard
# ---------------------------------------------------------------------------


class TestChangeConfigurationAbbGuard:
    """Pushing 22 OCPP measurands as MeterValuesSampledData crashes ABB."""

    @pytest.mark.asyncio
    async def test_safe_measurands_allowed(self, mock_websocket) -> None:
        # Build a CP whose self.call returns Accepted without hitting the WS.
        cp = FleetChargePoint(id="PILOT-01", connection=mock_websocket)
        cp.vendor = "ABB"
        cp.call = AsyncMock(return_value=MagicMock(status="Accepted"))

        safe_value = ",".join(sorted(_ABB_SAFE_MEASURANDS))
        result = await cp.change_configuration("MeterValuesSampledData", safe_value)
        assert result == "Accepted"

    @pytest.mark.asyncio
    async def test_unsafe_measurand_returns_not_supported(self, cp) -> None:
        # call() should never be invoked when the guard fires.
        cp.vendor = "ABB"
        cp.call = AsyncMock()
        result = await cp.change_configuration(
            "MeterValuesSampledData",
            "Energy.Active.Import.Register,Frequency,Temperature",
        )
        assert result == "NotSupported"
        cp.call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unsafe_measurand_rejected_before_boot_vendor_known(self, cp) -> None:
        cp.vendor = None
        cp.call = AsyncMock()
        result = await cp.change_configuration(
            "MeterValuesSampledData",
            "Energy.Active.Import.Register,Frequency",
        )
        assert result == "NotSupported"
        cp.call.assert_not_awaited()

    @pytest.mark.parametrize(
        "vendor",
        ["ABB", "ABB EV Solutions", "ABB Inc", "abb-terra"],
    )
    def test_abb_vendor_variants_require_guard(self, vendor: str) -> None:
        assert _requires_abb_safe_measurands(vendor)

    def test_non_abb_vendor_does_not_require_guard_after_boot(self) -> None:
        assert not _requires_abb_safe_measurands("Etrel")

    @pytest.mark.asyncio
    async def test_aligned_data_also_guarded(self, cp) -> None:
        cp.vendor = "ABB"
        cp.call = AsyncMock()
        result = await cp.change_configuration(
            "MeterValuesAlignedData",
            "Energy.Active.Import.Register,RPM",
        )
        assert result == "NotSupported"
        cp.call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_other_keys_unaffected(self, cp) -> None:
        cp.call = AsyncMock(return_value=MagicMock(status="Accepted"))
        result = await cp.change_configuration("HeartbeatInterval", "60")
        assert result == "Accepted"


# ---------------------------------------------------------------------------
# SendLocalList 16-entry cap
# ---------------------------------------------------------------------------


class TestSendLocalListCap:
    @pytest.mark.asyncio
    async def test_at_cap_passes_through(self, cp) -> None:
        cp.call = AsyncMock(return_value=MagicMock(status="Accepted"))
        entries = [
            {"id_tag": f"TAG{i:03d}", "id_tag_info": {"status": "Accepted"}}
            for i in range(_LOCAL_LIST_MAX_ENTRIES)
        ]
        result = await cp.send_local_list(
            list_version=1,
            update_type="Full",
            local_authorization_list=entries,
        )
        assert result == "Accepted"
        cp.call.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_over_cap_returns_not_supported(self, cp, caplog) -> None:
        cp.vendor = "ABB"
        cp.call = AsyncMock()
        entries = [
            {"id_tag": f"TAG{i:03d}", "id_tag_info": {"status": "Accepted"}}
            for i in range(_LOCAL_LIST_MAX_ENTRIES + 1)
        ]
        result = await cp.send_local_list(
            list_version=1,
            update_type="Full",
            local_authorization_list=entries,
        )
        assert result == "NotSupported"
        cp.call.assert_not_awaited()
        assert "exceeds" in caplog.text.lower() or "cap" in caplog.text.lower()

    @pytest.mark.asyncio
    async def test_non_abb_over_cap_is_allowed(self, cp) -> None:
        cp.vendor = "Etrel"
        cp.call = AsyncMock(return_value=MagicMock(status="Accepted"))
        entries = [
            {"id_tag": f"TAG{i:03d}", "id_tag_info": {"status": "Accepted"}}
            for i in range(_LOCAL_LIST_MAX_ENTRIES + 1)
        ]
        result = await cp.send_local_list(
            list_version=1,
            update_type="Full",
            local_authorization_list=entries,
        )
        assert result == "Accepted"
        cp.call.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_over_cap_rejected_before_boot_vendor_known(self, cp) -> None:
        cp.vendor = None
        cp.call = AsyncMock()
        entries = [
            {"id_tag": f"TAG{i:03d}", "id_tag_info": {"status": "Accepted"}}
            for i in range(_LOCAL_LIST_MAX_ENTRIES + 1)
        ]
        result = await cp.send_local_list(
            list_version=1,
            update_type="Full",
            local_authorization_list=entries,
        )
        assert result == "NotSupported"
        cp.call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_list_passes_through(self, cp) -> None:
        cp.call = AsyncMock(return_value=MagicMock(status="Accepted"))
        result = await cp.send_local_list(list_version=1, update_type="Full")
        assert result == "Accepted"
        cp.call.assert_awaited_once()


# ---------------------------------------------------------------------------
# OCPP16Session._on_authorize lookup
# ---------------------------------------------------------------------------


class TestOcpp16SessionAuthorize:
    @pytest.fixture()
    def session(self, mock_websocket):
        from src.websocket_handler.ocpp16_adapter import OCPP16Session
        from src.websocket_handler.rfid_authorization import RFIDAuthorizationService

        ts = MagicMock()
        ts.insert_telemetry_batch = AsyncMock()
        ts.lookup_id_tag = AsyncMock()
        ts.next_transaction_id = AsyncMock(return_value=1)
        ts.next_charging_profile_id = AsyncMock(return_value=1)
        mh = MagicMock()
        mh._push_to_main_api = AsyncMock()
        mh.rfid_authorization = RFIDAuthorizationService(ts, MagicMock())
        return OCPP16Session(
            station_id="PILOT-01",
            websocket=mock_websocket,
            timescale_client=ts,
            message_handler=mh,
        )

    @pytest.mark.asyncio
    async def test_known_id_tag_returns_accepted(self, session) -> None:
        session._timescale.lookup_id_tag.return_value = {
            "vehicle_id": "v1",
            "depot_id": "d1",
        }
        result = await session._on_authorize("PILOT-01", "DEADBEEF01")
        assert result == AuthorizationStatus.accepted
        session._timescale.lookup_id_tag.assert_awaited_once_with("DEADBEEF01", station_id="PILOT-01")

    @pytest.mark.asyncio
    async def test_unknown_id_tag_returns_invalid(self, session) -> None:
        session._timescale.lookup_id_tag.return_value = None
        result = await session._on_authorize("PILOT-01", "UNKNOWN-TAG")
        assert result == AuthorizationStatus.invalid

    @pytest.mark.asyncio
    async def test_db_failure_returns_invalid_not_accepted(self, session) -> None:
        """Fail closed: a DB outage must NOT silently authorize all tags."""
        session._timescale.lookup_id_tag.side_effect = RuntimeError("DB down")
        result = await session._on_authorize("PILOT-01", "ANYTAG")
        assert result == AuthorizationStatus.invalid

    @pytest.mark.asyncio
    async def test_send_charging_profile_draws_from_sequence(self, session) -> None:
        session._timescale.next_charging_profile_id.return_value = 7
        # Stub set_charging_profile so we can inspect the profile_id passed in.
        session._cp.set_charging_profile = AsyncMock(return_value=True)
        await session.send_charging_profile(
            evse_id=1,
            charging_profile={
                "chargingSchedule": {
                    "chargingRateUnit": "A",
                    "chargingSchedulePeriod": [
                        {"startPeriod": 0, "limit": 16, "numberPhases": 3}
                    ],
                },
            },
        )
        session._timescale.next_charging_profile_id.assert_awaited_once()
        kwargs = session._cp.set_charging_profile.call_args.kwargs
        assert kwargs["profile_id"] == 7

    def test_profile_id_fallback_counter_is_process_seeded(self, monkeypatch) -> None:
        from src.websocket_handler import ocpp16_adapter as adapter

        monkeypatch.setattr(adapter.time, "time_ns", lambda: 123)
        monkeypatch.setattr(adapter.secrets, "randbelow", lambda _: 456)

        counter = adapter._new_profile_id_fallback_counter()
        assert next(counter) == 579
