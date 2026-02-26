"""Unit tests for full OCPP 1.6 coverage — charge_point.py rewrite.

Tests all new incoming handlers (Heartbeat, Authorize, DataTransfer,
DiagnosticsStatusNotification, FirmwareStatusNotification) and all new
outgoing commands (ClearChargingProfile, GetCompositeSchedule, Reset,
ChangeAvailability, TriggerMessage, UnlockConnector, ChangeConfiguration,
GetConfiguration, ClearCache, SendLocalList, GetLocalListVersion,
ReserveNow, CancelReservation, UpdateFirmware, GetDiagnostics, DataTransfer).

Also validates fixes:
 - Transaction IDs are monotonic (no random collisions)
 - MeterValues parses Energy.Active.Import.Register
 - SetChargingProfile supports all purposes/kinds
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from ocpp.v16 import call_result

from src.adapters.ocpp import FleetChargePoint, convert_schedule_to_ocpp_profile

# ============ Fixtures ============


@pytest.fixture
def mock_ws():
    """Mock WebSocket connection."""
    ws = MagicMock()
    ws.send = AsyncMock()
    ws.recv = AsyncMock()
    ws.close = AsyncMock()
    return ws


@pytest.fixture
def cp(mock_ws):
    """FleetChargePoint with no callbacks."""
    return FleetChargePoint("test_cp", mock_ws)


@pytest.fixture
def cp_with_callbacks(mock_ws):
    """FleetChargePoint with all callbacks mocked."""
    return FleetChargePoint(
        "test_cp",
        mock_ws,
        on_status_change=AsyncMock(),
        on_meter_values=AsyncMock(),
        on_boot=AsyncMock(return_value=None),
        on_transaction_start=AsyncMock(return_value=None),
        on_transaction_stop=AsyncMock(),
        on_authorize=AsyncMock(return_value=None),
        on_diagnostics_status=AsyncMock(),
        on_firmware_status=AsyncMock(),
        on_data_transfer=AsyncMock(return_value=None),
    )


# ============ Incoming: BootNotification ============


@pytest.mark.asyncio
async def test_boot_notification_stores_metadata(cp):
    resp = await cp.on_boot_notification(
        "ABB",
        "Terra54",
        charge_point_serial_number="SN123",
        firmware_version="1.2.3",
    )
    assert resp.status == "Accepted"
    assert resp.interval == 300
    assert cp.vendor == "ABB"
    assert cp.model == "Terra54"
    assert cp.serial_number == "SN123"
    assert cp.firmware_version == "1.2.3"


# ============ Incoming: Heartbeat ============


@pytest.mark.asyncio
async def test_heartbeat_returns_time(cp):
    resp = await cp.on_heartbeat()
    assert isinstance(resp, call_result.Heartbeat)
    # current_time should be parseable ISO format
    datetime.fromisoformat(resp.current_time)


# ============ Incoming: StatusNotification ============


@pytest.mark.asyncio
async def test_status_notification_caches_state(cp):
    await cp.on_status_notification(1, "NoError", "Charging")
    assert cp.connector_status[1] == "Charging"


@pytest.mark.asyncio
async def test_status_notification_backward_compat_callback(mock_ws):
    """Old-style callback only takes (cp_id, connector, status)."""
    calls = []

    async def old_callback(cp_id, conn, status):
        calls.append((cp_id, conn, status))

    c = FleetChargePoint("cp1", mock_ws, on_status_change=old_callback)
    await c.on_status_notification(1, "NoError", "Available")
    assert len(calls) == 1
    assert calls[0] == ("cp1", 1, "Available")


# ============ Incoming: Authorize ============


@pytest.mark.asyncio
async def test_authorize_default_accepts(cp):
    resp = await cp.on_authorize_request("TAG001")
    assert resp.id_tag_info["status"] == "Accepted"


@pytest.mark.asyncio
async def test_authorize_callback_can_reject(mock_ws):
    async def reject(_cp_id, _tag):
        return "Invalid"

    c = FleetChargePoint("cp1", mock_ws, on_authorize=reject)
    resp = await c.on_authorize_request("BAD_TAG")
    assert resp.id_tag_info["status"] == "Invalid"


# ============ Incoming: DataTransfer ============


@pytest.mark.asyncio
async def test_data_transfer_default_accepts(cp):
    resp = await cp.on_data_transfer_request(
        "VendorX",
        message_id="msg1",
        data="hello",
    )
    assert resp.status == "Accepted"


@pytest.mark.asyncio
async def test_data_transfer_callback(mock_ws):
    async def handler(_cp, _vendor, _msg, _data):
        return ("Rejected", "nope")

    c = FleetChargePoint("cp1", mock_ws, on_data_transfer=handler)
    resp = await c.on_data_transfer_request("V", message_id="m", data="d")
    assert resp.status == "Rejected"
    assert resp.data == "nope"


# ============ Incoming: DiagnosticsStatusNotification ============


@pytest.mark.asyncio
async def test_diagnostics_status_notification(cp_with_callbacks):
    resp = await cp_with_callbacks.on_diagnostics_status_notification("Uploaded")
    assert isinstance(resp, call_result.DiagnosticsStatusNotification)
    cp_with_callbacks._cb_diagnostics.assert_awaited_once_with("test_cp", "Uploaded")


# ============ Incoming: FirmwareStatusNotification ============


@pytest.mark.asyncio
async def test_firmware_status_notification(cp_with_callbacks):
    resp = await cp_with_callbacks.on_firmware_status_notification("Downloaded")
    assert isinstance(resp, call_result.FirmwareStatusNotification)
    cp_with_callbacks._cb_firmware.assert_awaited_once_with("test_cp", "Downloaded")


# ============ Incoming: MeterValues — Energy.Active.Import.Register ============


@pytest.mark.asyncio
async def test_meter_values_parses_energy(mock_ws):
    """Energy.Active.Import.Register should now be parsed correctly."""
    received = {}

    async def cb(cp_id, conn, soc, power, energy, ts, tx_id, max_kw, raw):
        received.update(
            soc=soc,
            power=power,
            energy=energy,
            tx_id=tx_id,
            max_kw=max_kw,
        )

    c = FleetChargePoint("cp1", mock_ws, on_meter_values=cb)

    meter_value = [
        {
            "timestamp": "2026-01-15T10:00:00Z",
            "sampledValue": [
                {"measurand": "SoC", "value": "50"},
                {"measurand": "Power.Active.Import", "value": "22000", "unit": "W"},
                {"measurand": "Energy.Active.Import.Register", "value": "15400", "unit": "Wh"},
                {"measurand": "Power.Offered", "value": "50000", "unit": "W"},
            ],
        }
    ]

    await c.on_meter_values(1, meter_value)

    assert abs(received["soc"] - 0.50) < 0.001
    assert abs(received["power"] - 22.0) < 0.001
    assert abs(received["energy"] - 15.4) < 0.001
    assert abs(received["max_kw"] - 50.0) < 0.001


@pytest.mark.asyncio
async def test_meter_values_default_measurand(mock_ws):
    """Missing measurand key defaults to Energy.Active.Import.Register per OCPP spec."""
    received = {}

    async def cb(cp_id, conn, soc, power, energy, ts, tx_id, max_kw, raw):
        received["energy"] = energy

    c = FleetChargePoint("cp1", mock_ws, on_meter_values=cb)

    meter_value = [
        {
            "timestamp": "2026-01-15T10:00:00Z",
            "sampledValue": [
                {"value": "5000"},  # No measurand → defaults to Energy.Active.Import.Register
            ],
        }
    ]

    await c.on_meter_values(1, meter_value)
    assert abs(received["energy"] - 5.0) < 0.001  # 5000 Wh → 5.0 kWh


# ============ Incoming: Transaction IDs — monotonic ============


@pytest.mark.asyncio
async def test_transaction_ids_monotonic(cp):
    """Transaction IDs should be monotonically increasing, not random."""
    ids = []
    for _ in range(100):
        resp = await cp.on_start_transaction(
            1,
            "TAG",
            0,
            datetime.utcnow().isoformat(),
        )
        ids.append(resp.transaction_id)
        # Stop each transaction to reset
        await cp.on_stop_transaction(resp.transaction_id, 0, datetime.utcnow().isoformat())

    # All unique
    assert len(set(ids)) == 100
    # Monotonically increasing
    assert ids == sorted(ids)


@pytest.mark.asyncio
async def test_transaction_tracking_per_connector(cp):
    """Transactions should be tracked per connector."""
    r1 = await cp.on_start_transaction(1, "TAG1", 0, "2026-01-01T00:00:00Z")
    r2 = await cp.on_start_transaction(2, "TAG2", 0, "2026-01-01T00:00:00Z")

    assert r1.transaction_id != r2.transaction_id
    assert 1 in cp.transactions
    assert 2 in cp.transactions

    await cp.on_stop_transaction(r1.transaction_id, 100, "2026-01-01T01:00:00Z")
    assert 1 not in cp.transactions
    assert 2 in cp.transactions


# ============ Outgoing: SetChargingProfile — all purposes ============


@pytest.mark.asyncio
async def test_set_charging_profile_tx_default(cp):
    """SetChargingProfile should support TxDefaultProfile."""
    mock_resp = MagicMock(status="Accepted")
    cp.call = AsyncMock(return_value=mock_resp)

    result = await cp.set_charging_profile(
        connector_id=0,
        charging_schedule=[{"startPeriod": 0, "limit": 32000, "numberPhases": 3}],
        profile_purpose="TxDefaultProfile",
        profile_kind="Relative",
        stack_level=1,
    )
    assert result is True

    # Verify the profile payload
    sent = cp.call.call_args[0][0]
    profile = sent.cs_charging_profiles
    assert profile["charging_profile_purpose"] == "TxDefaultProfile"
    assert profile["charging_profile_kind"] == "Relative"
    assert profile["stack_level"] == 1


@pytest.mark.asyncio
async def test_set_charging_profile_max_profile(cp):
    """SetChargingProfile should support ChargePointMaxProfile."""
    mock_resp = MagicMock(status="Accepted")
    cp.call = AsyncMock(return_value=mock_resp)

    result = await cp.set_charging_profile(
        connector_id=0,
        charging_schedule=[{"startPeriod": 0, "limit": 100000, "numberPhases": 3}],
        profile_purpose="ChargePointMaxProfile",
        charging_rate_unit="W",
    )
    assert result is True


@pytest.mark.asyncio
async def test_set_charging_profile_recurring(cp):
    """SetChargingProfile should support Recurring profiles."""
    mock_resp = MagicMock(status="Accepted")
    cp.call = AsyncMock(return_value=mock_resp)

    result = await cp.set_charging_profile(
        connector_id=1,
        charging_schedule=[{"startPeriod": 0, "limit": 16, "numberPhases": 3}],
        profile_purpose="TxDefaultProfile",
        profile_kind="Recurring",
        charging_rate_unit="A",
        recurrency_kind="Daily",
        valid_from="2026-01-01T00:00:00Z",
        valid_to="2026-12-31T23:59:59Z",
    )
    assert result is True
    profile = cp.call.call_args[0][0].cs_charging_profiles
    assert profile["recurrency_kind"] == "Daily"
    assert profile["valid_from"] == "2026-01-01T00:00:00Z"


# ============ Outgoing: ClearChargingProfile ============


@pytest.mark.asyncio
async def test_clear_charging_profile(cp):
    mock_resp = MagicMock(status="Accepted")
    cp.call = AsyncMock(return_value=mock_resp)

    result = await cp.clear_charging_profile(profile_id=1, connector_id=1)
    assert result == "Accepted"


@pytest.mark.asyncio
async def test_clear_charging_profile_all(cp):
    mock_resp = MagicMock(status="Accepted")
    cp.call = AsyncMock(return_value=mock_resp)

    result = await cp.clear_charging_profile()
    assert result == "Accepted"


# ============ Outgoing: GetCompositeSchedule ============


@pytest.mark.asyncio
async def test_get_composite_schedule(cp):
    mock_resp = MagicMock(
        status="Accepted",
        connector_id=1,
        schedule_start="2026-01-15T10:00:00Z",
        charging_schedule={"charging_rate_unit": "W"},
    )
    cp.call = AsyncMock(return_value=mock_resp)

    result = await cp.get_composite_schedule(1, 3600, "W")
    assert result is not None
    assert result["status"] == "Accepted"
    assert result["connector_id"] == 1


@pytest.mark.asyncio
async def test_get_composite_schedule_rejected(cp):
    mock_resp = MagicMock(status="Rejected")
    cp.call = AsyncMock(return_value=mock_resp)

    result = await cp.get_composite_schedule(1, 3600)
    assert result is None


# ============ Outgoing: Reset ============


@pytest.mark.asyncio
async def test_reset_soft(cp):
    mock_resp = MagicMock(status="Accepted")
    cp.call = AsyncMock(return_value=mock_resp)

    result = await cp.reset("Soft")
    assert result == "Accepted"


@pytest.mark.asyncio
async def test_reset_hard(cp):
    mock_resp = MagicMock(status="Accepted")
    cp.call = AsyncMock(return_value=mock_resp)

    result = await cp.reset("Hard")
    assert result == "Accepted"


# ============ Outgoing: ChangeAvailability ============


@pytest.mark.asyncio
async def test_change_availability(cp):
    mock_resp = MagicMock(status="Accepted")
    cp.call = AsyncMock(return_value=mock_resp)

    result = await cp.change_availability(1, "Inoperative")
    assert result == "Accepted"


# ============ Outgoing: TriggerMessage ============


@pytest.mark.asyncio
async def test_trigger_message(cp):
    mock_resp = MagicMock(status="Accepted")
    cp.call = AsyncMock(return_value=mock_resp)

    result = await cp.trigger_message("MeterValues", connector_id=1)
    assert result == "Accepted"


# ============ Outgoing: UnlockConnector ============


@pytest.mark.asyncio
async def test_unlock_connector(cp):
    mock_resp = MagicMock(status="Unlocked")
    cp.call = AsyncMock(return_value=mock_resp)

    result = await cp.unlock_connector(1)
    assert result == "Unlocked"


# ============ Outgoing: ChangeConfiguration ============


@pytest.mark.asyncio
async def test_change_configuration(cp):
    mock_resp = MagicMock(status="Accepted")
    cp.call = AsyncMock(return_value=mock_resp)

    result = await cp.change_configuration("MeterValueSampleInterval", "30")
    assert result == "Accepted"


# ============ Outgoing: GetConfiguration ============


@pytest.mark.asyncio
async def test_get_configuration(cp):
    mock_resp = MagicMock(
        configuration_key=[{"key": "HeartbeatInterval", "value": "300"}],
        unknown_key=[],
    )
    cp.call = AsyncMock(return_value=mock_resp)

    result = await cp.get_configuration(["HeartbeatInterval"])
    assert len(result["configuration_key"]) == 1
    assert result["configuration_key"][0]["key"] == "HeartbeatInterval"


# ============ Outgoing: ClearCache ============


@pytest.mark.asyncio
async def test_clear_cache(cp):
    mock_resp = MagicMock(status="Accepted")
    cp.call = AsyncMock(return_value=mock_resp)
    assert await cp.clear_cache() == "Accepted"


# ============ Outgoing: SendLocalList / GetLocalListVersion ============


@pytest.mark.asyncio
async def test_send_local_list(cp):
    mock_resp = MagicMock(status="Accepted")
    cp.call = AsyncMock(return_value=mock_resp)

    result = await cp.send_local_list(
        1,
        "Full",
        [{"id_tag": "TAG1", "id_tag_info": {"status": "Accepted"}}],
    )
    assert result == "Accepted"


@pytest.mark.asyncio
async def test_get_local_list_version(cp):
    mock_resp = MagicMock(list_version=5)
    cp.call = AsyncMock(return_value=mock_resp)
    assert await cp.get_local_list_version() == 5


# ============ Outgoing: ReserveNow / CancelReservation ============


@pytest.mark.asyncio
async def test_reserve_now(cp):
    mock_resp = MagicMock(status="Accepted")
    cp.call = AsyncMock(return_value=mock_resp)

    result = await cp.reserve_now(1, "2026-01-15T12:00:00Z", "TAG1", 42)
    assert result == "Accepted"


@pytest.mark.asyncio
async def test_cancel_reservation(cp):
    mock_resp = MagicMock(status="Accepted")
    cp.call = AsyncMock(return_value=mock_resp)
    assert await cp.cancel_reservation(42) == "Accepted"


# ============ Outgoing: UpdateFirmware / GetDiagnostics ============


@pytest.mark.asyncio
async def test_update_firmware(cp):
    cp.call = AsyncMock()
    await cp.update_firmware(
        "https://firmware.example.com/v2.bin",
        "2026-01-15T03:00:00Z",
        retries=3,
        retry_interval=60,
    )
    cp.call.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_diagnostics(cp):
    mock_resp = MagicMock(file_name="diag_2026.tar.gz")
    cp.call = AsyncMock(return_value=mock_resp)

    result = await cp.get_diagnostics(
        "ftp://logs.example.com/upload",
        start_time="2026-01-01T00:00:00Z",
        stop_time="2026-01-15T00:00:00Z",
    )
    assert result == "diag_2026.tar.gz"


# ============ Outgoing: DataTransfer (CSMS → CP) ============


@pytest.mark.asyncio
async def test_outgoing_data_transfer(cp):
    mock_resp = MagicMock(status="Accepted", data="ok")
    cp.call = AsyncMock(return_value=mock_resp)

    status, data = await cp.data_transfer("VendorX", message_id="ping", data="hello")
    assert status == "Accepted"
    assert data == "ok"


# ============ Error handling ============


@pytest.mark.asyncio
async def test_outgoing_command_error_returns_safe_default(cp):
    """All outgoing methods should return safe defaults on exception."""
    cp.call = AsyncMock(side_effect=Exception("connection lost"))

    assert await cp.reset() == "Rejected"
    assert await cp.change_availability(1) == "Rejected"
    assert await cp.trigger_message("Heartbeat") == "Rejected"
    assert await cp.unlock_connector(1) == "UnlockFailed"
    assert await cp.clear_cache() == "Rejected"
    assert await cp.clear_charging_profile() == "Unknown"
    assert await cp.get_composite_schedule(1, 3600) is None
    assert await cp.get_local_list_version() == -1
    assert await cp.reserve_now(1, "2026-01-01T00:00:00Z", "T", 1) == "Rejected"
    assert await cp.cancel_reservation(1) == "Rejected"
    status, data = await cp.data_transfer("V")
    assert status == "Rejected"


# ============ Schedule conversion: Amps mode ============


def test_schedule_conversion_amps():
    """convert_schedule_to_ocpp_profile should handle 'A' rate unit."""
    schedule = [(0, 11.0)]  # 11 kW
    profile = convert_schedule_to_ocpp_profile(
        schedule,
        number_phases=3,
        charging_rate_unit="A",
    )
    # 11000W / (3 * 230V) ≈ 15.94 A
    assert abs(profile[0]["limit"] - 15.942) < 0.1
    assert profile[0]["numberPhases"] == 3


def test_schedule_conversion_watts():
    """Existing Watts conversion still works."""
    schedule = [(0, 80.0), (1, 60.0)]
    profile = convert_schedule_to_ocpp_profile(schedule, delta_t=0.25)
    assert profile[0]["limit"] == 80000
    assert profile[1]["limit"] == 60000
    assert profile[0]["startPeriod"] == 0
    assert profile[1]["startPeriod"] == 900
