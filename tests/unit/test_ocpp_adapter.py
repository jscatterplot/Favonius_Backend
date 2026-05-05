"""Unit tests for OCPP adapter.

Reference: Development plan Step 3.1, PRD.md#11-2-unit-test-requirements
"""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from ocpp.v16 import call_result

from src.adapters.ocpp import (
    FleetChargePoint,
    OCPPServer,
    convert_schedule_to_ocpp_profile,
    dispatch_charging_profiles,
    store_meter_values,
)
from src.core.models import OptimizationResult

# ============ Fixtures ============


@pytest.fixture
def mock_websocket():
    """Mock WebSocket connection."""
    ws = MagicMock()
    ws.send = AsyncMock()
    ws.recv = AsyncMock()
    ws.close = AsyncMock()
    return ws


@pytest.fixture
def sample_charge_point_id():
    """Sample charge point ID."""
    return "charger_001"


@pytest.fixture
def sample_optimization_result():
    """Sample optimization result."""
    return OptimizationResult(
        run_id=uuid4(),
        status="completed",
        objective_value=1000.0,
        solve_time=5.0,
        schedule={
            "bus_1": {
                "charging_power": [80.0, 60.0, 80.0, 0.0],
                "soc": [0.3, 0.5, 0.7, 0.9],
            },
            "bus_2": {
                "charging_power": [70.0, 70.0, 0.0, 0.0],
                "soc": [0.4, 0.6, 0.8, 0.95],
            },
        },
        battery_dispatch=[10.0, -5.0, 0.0, 0.0],
        grid_power=[200.0, 150.0, 100.0, 50.0],
        peak_demand=200.0,
    )


# ============ Charge Point Tests ============


def test_fleet_charge_point_initialization(mock_websocket, sample_charge_point_id):
    """Test FleetChargePoint initialization."""
    cp = FleetChargePoint(sample_charge_point_id, mock_websocket)
    assert cp.id == sample_charge_point_id
    assert cp.current_transaction_id is None
    assert cp.on_status_change is None
    assert cp.on_meter_values_callback is None


@pytest.mark.asyncio
async def test_boot_notification_handler(mock_websocket, sample_charge_point_id):
    """Test BootNotification handler."""
    cp = FleetChargePoint(sample_charge_point_id, mock_websocket)
    response = await cp.on_boot_notification("Vendor", "Model")

    assert isinstance(response, call_result.BootNotification)
    assert response.status == "Accepted"
    assert response.interval == 300


@pytest.mark.asyncio
async def test_boot_notification_invokes_callback_without_kwarg_clash(
    mock_websocket, sample_charge_point_id
):
    """Regression: ``firmware_version`` must not be passed twice to the callback.

    The python-ocpp library forwards every BootNotification field into kwargs
    (snake-cased), including ``firmware_version``. Passing it positionally AND
    via ``**kwargs`` raised ``TypeError: multiple values for argument
    'firmware_version'`` and silently swallowed the cross-restart recovery
    work in OCPP16Session._on_boot.
    """
    captured = {}

    async def cb(cp_id, vendor, model, serial_number, firmware_version, **kwargs):
        captured.update(
            cp_id=cp_id,
            vendor=vendor,
            model=model,
            serial_number=serial_number,
            firmware_version=firmware_version,
            extra_kwargs=kwargs,
        )

    cp = FleetChargePoint(sample_charge_point_id, mock_websocket, on_boot=cb)
    response = await cp.on_boot_notification(
        "ABB",
        "TerraAC",
        charge_point_serial_number="TACW1141622G1433",
        firmware_version="V1.8.36",
        iccid="89000000000000000000",
    )

    assert isinstance(response, call_result.BootNotification)
    assert captured["firmware_version"] == "V1.8.36"
    assert captured["serial_number"] == "TACW1141622G1433"
    # firmware_version must be stripped from kwargs to prevent the TypeError;
    # other extra fields (charge_point_serial_number, iccid, ...) must survive.
    assert "firmware_version" not in captured["extra_kwargs"]
    assert captured["extra_kwargs"].get("iccid") == "89000000000000000000"


@pytest.mark.asyncio
async def test_security_event_notification_handler(mock_websocket, sample_charge_point_id):
    """ABB Terra AC sends StartupOfTheDevice / SettingSystemTime; we must ack."""
    cp = FleetChargePoint(sample_charge_point_id, mock_websocket)
    response = await cp.on_security_event_notification(
        type="StartupOfTheDevice",
        timestamp="2026-05-04T15:43:41.000Z",
        tech_info="ocppBoot",
    )
    assert isinstance(response, call_result.SecurityEventNotification)


@pytest.mark.asyncio
async def test_status_notification_handler(mock_websocket, sample_charge_point_id):
    """Test StatusNotification handler with callback."""
    callback_called = False
    callback_data = None

    async def status_callback(cp_id, conn_id, status):
        nonlocal callback_called, callback_data
        callback_called = True
        callback_data = (cp_id, conn_id, status)

    cp = FleetChargePoint(sample_charge_point_id, mock_websocket, on_status_change=status_callback)
    response = await cp.on_status_notification(1, "NoError", "Available")

    assert isinstance(response, call_result.StatusNotification)
    assert callback_called
    assert callback_data == (sample_charge_point_id, 1, "Available")


@pytest.mark.asyncio
async def test_meter_values_handler(mock_websocket, sample_charge_point_id):
    """Test MeterValues handler with SoC and power extraction."""
    callback_called = False
    callback_data = None

    async def meter_callback(cp_id, conn_id, soc, power, timestamp, max_charge_kw=None):
        nonlocal callback_called, callback_data
        callback_called = True
        callback_data = (cp_id, conn_id, soc, power, timestamp)

    cp = FleetChargePoint(sample_charge_point_id, mock_websocket, on_meter_values=meter_callback)

    meter_value = [
        {
            "timestamp": datetime.utcnow().isoformat(),
            "sampledValue": [
                {"measurand": "SoC", "value": "75.5", "unit": ""},
                {"measurand": "Power.Active.Import", "value": "8000", "unit": "W"},
            ],
        }
    ]

    response = await cp.on_meter_values(1, meter_value)

    assert isinstance(response, call_result.MeterValues)
    assert callback_called
    assert callback_data[0] == sample_charge_point_id
    assert callback_data[1] == 1
    assert abs(callback_data[2] - 0.755) < 0.001  # SoC: 75.5% -> 0.755
    assert abs(callback_data[3] - 8.0) < 0.001  # Power: 8000W -> 8.0kW


@pytest.mark.asyncio
async def test_set_charging_profile(mock_websocket, sample_charge_point_id):
    """Test SetChargingProfile command."""
    cp = FleetChargePoint(sample_charge_point_id, mock_websocket)

    # Mock the call method
    mock_response = MagicMock()
    mock_response.status = "Accepted"
    cp.call = AsyncMock(return_value=mock_response)

    schedule = [
        {"startPeriod": 0, "limit": 80000, "numberPhases": 3},
        {"startPeriod": 900, "limit": 60000, "numberPhases": 3},
    ]

    result = await cp.set_charging_profile(1, schedule, profile_id=123)

    assert result is True
    cp.call.assert_called_once()


@pytest.mark.asyncio
async def test_set_charging_profile_default_profile_id_still_works(
    mock_websocket, sample_charge_point_id
):
    """Keep backward-compatible default profile_id for existing callers."""
    cp = FleetChargePoint(sample_charge_point_id, mock_websocket)
    mock_response = MagicMock()
    mock_response.status = "Accepted"
    cp.call = AsyncMock(return_value=mock_response)
    result = await cp.set_charging_profile(
        1,
        [{"startPeriod": 0, "limit": 10, "numberPhases": 1}],
    )
    assert result is True


@pytest.mark.asyncio
async def test_set_charging_profile_rejects_relative_chargepointmaxprofile(
    mock_websocket, sample_charge_point_id
):
    """Relative ChargePointMaxProfile is forbidden by OCPP 1.6."""
    cp = FleetChargePoint(sample_charge_point_id, mock_websocket)
    with pytest.raises(ValueError, match="forbidden"):
        await cp.set_charging_profile(
            1,
            [{"startPeriod": 0, "limit": 10, "numberPhases": 1}],
            profile_id=42,
            profile_purpose="ChargePointMaxProfile",
            profile_kind="Relative",
        )


@pytest.mark.asyncio
async def test_route_message_invokes_on_message_received(mock_websocket, sample_charge_point_id):
    """Liveness hook fires for every received OCPP frame.

    The websocket-handler stale-connection sweeper relies on this to keep
    OCPP 1.6 sockets alive when the charger sends only StatusNotification
    or MeterValues between Heartbeats.
    """
    received = []

    async def on_msg() -> None:
        received.append(True)

    cp = FleetChargePoint(
        sample_charge_point_id,
        mock_websocket,
        on_message_received=on_msg,
    )
    # Bypass the upstream library router; we only care that the hook fires.
    with patch("ocpp.v16.ChargePoint.route_message", new=AsyncMock()):
        await cp.route_message('[2,"abc","Heartbeat",{}]')
        await cp.route_message('[2,"def","StatusNotification",{}]')

    assert len(received) == 2


@pytest.mark.asyncio
async def test_route_message_swallows_callback_exceptions(mock_websocket, sample_charge_point_id):
    """A failing liveness hook must never break message routing."""

    async def on_msg() -> None:
        raise RuntimeError("connection_manager unavailable")

    cp = FleetChargePoint(
        sample_charge_point_id,
        mock_websocket,
        on_message_received=on_msg,
    )
    with patch("ocpp.v16.ChargePoint.route_message", new=AsyncMock()) as upstream:
        await cp.route_message('[2,"abc","Heartbeat",{}]')
        upstream.assert_awaited_once()


@pytest.mark.asyncio
async def test_route_message_works_without_on_message_received(
    mock_websocket, sample_charge_point_id
):
    """The hook is optional — existing call sites must keep working."""
    cp = FleetChargePoint(sample_charge_point_id, mock_websocket)
    with patch("ocpp.v16.ChargePoint.route_message", new=AsyncMock()) as upstream:
        await cp.route_message('[2,"abc","Heartbeat",{}]')
        upstream.assert_awaited_once()


@pytest.mark.asyncio
async def test_remote_start_stop_transaction(mock_websocket, sample_charge_point_id):
    """Test RemoteStartTransaction and RemoteStopTransaction."""
    cp = FleetChargePoint(sample_charge_point_id, mock_websocket)

    # Mock the call method
    mock_response = MagicMock()
    mock_response.status = "Accepted"
    cp.call = AsyncMock(return_value=mock_response)

    # Test remote start
    result = await cp.remote_start_transaction(1, "TAG001")
    assert result is True

    # Test remote stop
    result = await cp.remote_stop_transaction(12345)
    assert result is True

    assert cp.call.call_count == 2


def test_charging_profile_conversion():
    """Test schedule to OCPP profile conversion."""
    schedule = [(0, 80.0), (1, 60.0), (2, 80.0)]
    profile = convert_schedule_to_ocpp_profile(schedule, delta_t=0.25)

    assert len(profile) == 3
    assert profile[0]["startPeriod"] == 0
    assert profile[0]["limit"] == 80000  # 80kW * 1000
    assert profile[1]["startPeriod"] == 900  # 1 * 0.25 * 3600
    assert profile[1]["limit"] == 60000  # 60kW * 1000
    assert profile[2]["startPeriod"] == 1800  # 2 * 0.25 * 3600
    assert all(p["numberPhases"] == 3 for p in profile)


# ============ Server Tests ============


@pytest.mark.asyncio
async def test_ocpp_server_initialization():
    """Test OCPPServer initialization."""
    server = OCPPServer(host="127.0.0.1", port=9001)
    assert server.host == "127.0.0.1"
    assert server.port == 9001
    assert len(server.charge_points) == 0
    assert server.is_connected("test_id") is False


@pytest.mark.asyncio
async def test_connection_handling(mock_websocket):
    """Test connection handling."""
    server = OCPPServer()

    # Mock websocket path
    mock_websocket.path = "/charger_001"

    # Mock charge point start to avoid infinite loop
    with patch.object(FleetChargePoint, "start", new_callable=AsyncMock) as mock_start:
        mock_start.side_effect = asyncio.CancelledError()  # Simulate disconnect

        try:
            await server.on_connect(mock_websocket, "/charger_001")
        except asyncio.CancelledError:
            pass  # Expected

        # Verify charge point was registered and cleaned up
        assert "charger_001" not in server.charge_points


@pytest.mark.asyncio
async def test_charge_point_registration(mock_websocket):
    """Test charge point registration."""
    server = OCPPServer()

    # Manually register a charge point
    cp = FleetChargePoint("test_charger", mock_websocket)
    server.charge_points["test_charger"] = cp

    assert server.is_connected("test_charger") is True
    assert server.get_charge_point("test_charger") == cp
    assert server.get_charge_point("nonexistent") is None


@pytest.mark.asyncio
async def test_disconnect_cleanup(mock_websocket):
    """Test cleanup on disconnect."""
    server = OCPPServer()
    server.charge_points["test_charger"] = FleetChargePoint("test_charger", mock_websocket)

    await server.stop()

    assert len(server.charge_points) == 0


# ============ Integration Tests ============


@pytest.mark.asyncio
async def test_end_to_end_connection(mock_websocket):
    """Test end-to-end connection flow."""
    server = OCPPServer()

    # Mock charge point start
    with patch.object(FleetChargePoint, "start", new_callable=AsyncMock) as mock_start:
        mock_start.side_effect = asyncio.CancelledError()

        try:
            await server.on_connect(mock_websocket, "/charger_001")
        except asyncio.CancelledError:
            pass

        # Verify connection was attempted
        assert mock_start.called


@pytest.mark.asyncio
async def test_meter_values_storage():
    """Test meter values storage in database."""
    mock_pool = MagicMock()
    mock_conn = AsyncMock()
    mock_pool.acquire = MagicMock(return_value=mock_conn)
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=None)
    mock_pool.static = mock_pool
    mock_pool.ts = mock_pool

    timestamp = datetime.utcnow()
    await store_meter_values(mock_pool, "charger_001", 1, 0.75, 8.0, timestamp, vehicle_id="bus_1")

    mock_conn.execute.assert_called_once()


@pytest.mark.skip(
    reason="Session 3: dispatch is now queue-mediated (not in-process). "
    "See tests/unit/test_ocpp_dispatch.py and tests/integration/test_dispatch_queue.py."
)
@pytest.mark.asyncio
async def test_charging_profile_dispatch(sample_optimization_result, mock_websocket):
    """Legacy in-process dispatch test — superseded by the queue path."""


@pytest.mark.skip(
    reason="Session 3: dispatch is now queue-mediated (not in-process). "
    "See tests/unit/test_ocpp_dispatch.py and tests/integration/test_dispatch_queue.py."
)
@pytest.mark.asyncio
async def test_charging_profile_dispatch_missing_charger(
    sample_optimization_result, mock_websocket
):
    """Legacy in-process dispatch test — superseded by the queue path."""


@pytest.mark.skip(
    reason="Session 3: dispatch is now queue-mediated (not in-process). "
    "See tests/unit/test_ocpp_dispatch.py and tests/integration/test_dispatch_queue.py."
)
@pytest.mark.asyncio
async def test_charging_profile_dispatch_missing_vehicle(
    sample_optimization_result, mock_websocket
):
    """Legacy in-process dispatch test — superseded by the queue path."""
