"""Full OCPP protocol integration tests.

Tests complete OCPP 1.6 message exchanges, SetChargingProfile acceptance/rejection,
MeterValues SoC extraction, multiple charger connections, and connection
timeout/recovery scenarios.

Reference: Development plan Step 3.1, PRD.md#9-1-ocpp-integration
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from websockets.server import WebSocketServerProtocol

from src.adapters.ocpp.charge_point import (
    FleetChargePoint,
    convert_schedule_to_ocpp_profile,
)
from src.adapters.ocpp.server import OCPPServer

# ============ Fixtures ============


@pytest.fixture
def mock_websocket():
    """Create a mock WebSocket connection."""
    ws = AsyncMock(spec=WebSocketServerProtocol)
    ws.send = AsyncMock()
    ws.recv = AsyncMock()
    ws.close = AsyncMock()
    ws.open = True
    ws.subprotocol = "ocpp1.6"
    return ws


@pytest.fixture
def ocpp_server():
    """Create an OCPP server instance."""
    return OCPPServer(host="127.0.0.1", port=9001)


@pytest.fixture
def charge_point(mock_websocket):
    """Create a FleetChargePoint instance."""
    return FleetChargePoint(
        id="test_charger",
        connection=mock_websocket,
        on_status_change=AsyncMock(),
        on_meter_values=AsyncMock(),
    )


def create_ocpp_call(action: str, payload: dict, unique_id: str = None) -> str:
    """Create an OCPP Call message."""
    unique_id = unique_id or str(uuid4())[:8]
    return json.dumps([2, unique_id, action, payload])


def create_ocpp_call_result(unique_id: str, payload: dict) -> str:
    """Create an OCPP CallResult message."""
    return json.dumps([3, unique_id, payload])


def create_ocpp_call_error(unique_id: str, error_code: str, error_desc: str) -> str:
    """Create an OCPP CallError message."""
    return json.dumps([4, unique_id, error_code, error_desc, {}])


# ============ Full Message Exchange Tests ============


class TestFullMessageExchange:
    """Tests for complete OCPP message exchange flows."""

    @pytest.mark.asyncio
    async def test_boot_notification_flow(self, charge_point):
        """Test complete BootNotification flow."""
        # Simulate incoming BootNotification
        response = await charge_point.on_boot_notification(
            charge_point_vendor="TestVendor",
            charge_point_model="TestModel",
            charge_point_serial_number="SN123456",
            firmware_version="1.0.0",
        )

        assert response.status == "Accepted"
        assert response.interval == 300
        assert response.current_time is not None

    @pytest.mark.asyncio
    async def test_status_notification_flow(self, charge_point):
        """Test complete StatusNotification flow."""
        # Simulate incoming StatusNotification
        response = await charge_point.on_status_notification(
            connector_id=1,
            error_code="NoError",
            status="Available",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        # StatusNotification should return empty response
        assert response is not None

        # Callback should have been called
        charge_point.on_status_change.assert_called_once()

    @pytest.mark.asyncio
    async def test_meter_values_flow(self, charge_point):
        """Test complete MeterValues flow with SoC extraction."""
        ts = datetime.now(timezone.utc).isoformat()

        meter_value = [
            {
                "timestamp": ts,
                "sampledValue": [
                    {
                        "measurand": "SoC",
                        "value": "75",
                        "unit": "Percent",
                    },
                    {
                        "measurand": "Power.Active.Import",
                        "value": "50000",
                        "unit": "W",
                    },
                    {
                        "measurand": "Energy.Active.Import.Register",
                        "value": "15000",
                        "unit": "Wh",
                    },
                ],
            }
        ]

        response = await charge_point.on_meter_values(
            connector_id=1,
            meter_value=meter_value,
            transaction_id=12345,
        )

        assert response is not None

        # Verify callback was called with correct values
        charge_point.on_meter_values.assert_called_once()
        args = charge_point.on_meter_values.call_args[0]
        assert args[0] == "test_charger"  # charge_point_id
        assert args[1] == 1  # connector_id
        assert args[2] == 0.75  # SoC (converted from percentage)
        assert args[3] == 50.0  # Power in kW (converted from W)

    @pytest.mark.asyncio
    async def test_transaction_flow(self, charge_point):
        """Test complete transaction flow: Start -> MeterValues -> Stop."""
        # Start transaction
        start_response = await charge_point.on_start_transaction(
            connector_id=1,
            id_tag="USER123",
            meter_start=0,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        assert start_response.id_tag_info["status"] == "Accepted"
        transaction_id = start_response.transaction_id
        assert transaction_id is not None

        # Send meter values during transaction
        ts = datetime.now(timezone.utc).isoformat()
        await charge_point.on_meter_values(
            connector_id=1,
            meter_value=[
                {
                    "timestamp": ts,
                    "sampledValue": [
                        {"measurand": "SoC", "value": "50", "unit": "Percent"},
                    ],
                }
            ],
            transaction_id=transaction_id,
        )

        # Stop transaction
        stop_response = await charge_point.on_stop_transaction(
            transaction_id=transaction_id,
            id_tag="USER123",
            meter_stop=10000,
            timestamp=datetime.now(timezone.utc).isoformat(),
            reason="Local",
        )

        assert stop_response.id_tag_info["status"] == "Accepted"


# ============ SetChargingProfile Tests ============


class TestSetChargingProfile:
    """Tests for SetChargingProfile message handling."""

    @pytest.mark.asyncio
    async def test_set_charging_profile_accepted(self, charge_point):
        """Test SetChargingProfile accepted by charger."""
        mock_response = MagicMock()
        mock_response.status = "Accepted"
        charge_point.call = AsyncMock(return_value=mock_response)

        schedule = [
            {"startPeriod": 0, "limit": 80000, "numberPhases": 3},
            {"startPeriod": 900, "limit": 60000, "numberPhases": 3},
            {"startPeriod": 1800, "limit": 40000, "numberPhases": 3},
        ]

        result = await charge_point.set_charging_profile(1, schedule)

        assert result is True
        charge_point.call.assert_called_once()

        # Verify payload structure
        call_args = charge_point.call.call_args[0][0]
        assert call_args.connector_id == 1
        assert "cs_charging_profiles" in dir(call_args)

    @pytest.mark.asyncio
    async def test_set_charging_profile_rejected(self, charge_point):
        """Test SetChargingProfile rejected by charger."""
        mock_response = MagicMock()
        mock_response.status = "Rejected"
        charge_point.call = AsyncMock(return_value=mock_response)

        schedule = [{"startPeriod": 0, "limit": 80000, "numberPhases": 3}]

        # max_retries=1 to speed up test
        result = await charge_point.set_charging_profile(1, schedule, max_retries=1)

        assert result is False

    @pytest.mark.asyncio
    async def test_set_charging_profile_not_supported(self, charge_point):
        """Test SetChargingProfile when charger doesn't support it."""
        mock_response = MagicMock()
        mock_response.status = "NotSupported"
        charge_point.call = AsyncMock(return_value=mock_response)

        schedule = [{"startPeriod": 0, "limit": 80000, "numberPhases": 3}]

        result = await charge_point.set_charging_profile(1, schedule, max_retries=1)

        assert result is False

    @pytest.mark.asyncio
    async def test_set_charging_profile_complex_schedule(self, charge_point):
        """Test SetChargingProfile with complex schedule."""
        mock_response = MagicMock()
        mock_response.status = "Accepted"
        charge_point.call = AsyncMock(return_value=mock_response)

        # 24-hour schedule with 15-minute intervals
        schedule = []
        for i in range(96):
            power = 80000 if i < 48 else 0  # Charge first 12 hours only
            schedule.append(
                {
                    "startPeriod": i * 900,  # 15 minutes in seconds
                    "limit": power,
                    "numberPhases": 3,
                }
            )

        result = await charge_point.set_charging_profile(1, schedule)

        assert result is True


# ============ MeterValues SoC Extraction Tests ============


class TestMeterValuesSoCExtraction:
    """Tests for MeterValues SoC extraction edge cases."""

    @pytest.mark.asyncio
    async def test_soc_in_percentage(self, charge_point):
        """Test SoC extraction from percentage value."""
        ts = datetime.now(timezone.utc).isoformat()
        meter_value = [
            {
                "timestamp": ts,
                "sampledValue": [
                    {"measurand": "SoC", "value": "85", "unit": "Percent"},
                ],
            }
        ]

        await charge_point.on_meter_values(connector_id=1, meter_value=meter_value)

        args = charge_point.on_meter_values.call_args[0]
        assert args[2] == 0.85

    @pytest.mark.asyncio
    async def test_soc_without_unit(self, charge_point):
        """Test SoC extraction when unit is missing."""
        ts = datetime.now(timezone.utc).isoformat()
        meter_value = [
            {
                "timestamp": ts,
                "sampledValue": [
                    {"measurand": "SoC", "value": "60"},  # No unit specified
                ],
            }
        ]

        await charge_point.on_meter_values(connector_id=1, meter_value=meter_value)

        args = charge_point.on_meter_values.call_args[0]
        assert args[2] == 0.60

    @pytest.mark.asyncio
    async def test_power_in_different_units(self, charge_point):
        """Test power extraction with different units."""
        ts = datetime.now(timezone.utc).isoformat()

        # Test with kW
        meter_value_kw = [
            {
                "timestamp": ts,
                "sampledValue": [
                    {"measurand": "SoC", "value": "50", "unit": "Percent"},
                    {"measurand": "Power.Active.Import", "value": "50", "unit": "kW"},
                ],
            }
        ]

        await charge_point.on_meter_values(connector_id=1, meter_value=meter_value_kw)

        args = charge_point.on_meter_values.call_args[0]
        assert args[3] == 50.0

    @pytest.mark.asyncio
    async def test_multiple_meter_value_entries(self, charge_point):
        """Test processing multiple meter value entries."""
        ts = datetime.now(timezone.utc)

        meter_value = [
            {
                "timestamp": ts.isoformat(),
                "sampledValue": [
                    {"measurand": "SoC", "value": "50", "unit": "Percent"},
                ],
            },
            {
                "timestamp": (ts + timedelta(minutes=1)).isoformat(),
                "sampledValue": [
                    {"measurand": "Power.Active.Import", "value": "75000", "unit": "W"},
                ],
            },
        ]

        await charge_point.on_meter_values(connector_id=1, meter_value=meter_value)

        # Latest values should be used
        args = charge_point.on_meter_values.call_args[0]
        assert args[2] == 0.50  # SoC
        assert args[3] == 75.0  # Power


# ============ Multiple Charger Connection Tests ============


class TestMultipleChargerConnections:
    """Tests for multiple charger connections."""

    @pytest.mark.asyncio
    async def test_register_multiple_chargers(self, ocpp_server):
        """Test registering multiple charge points."""
        mock_ws1 = AsyncMock()
        mock_ws2 = AsyncMock()
        mock_ws3 = AsyncMock()

        # Manually register charge points
        cp1 = FleetChargePoint(id="charger_001", connection=mock_ws1)
        cp2 = FleetChargePoint(id="charger_002", connection=mock_ws2)
        cp3 = FleetChargePoint(id="charger_003", connection=mock_ws3)

        ocpp_server.charge_points["charger_001"] = cp1
        ocpp_server.charge_points["charger_002"] = cp2
        ocpp_server.charge_points["charger_003"] = cp3

        assert len(ocpp_server.charge_points) == 3
        assert ocpp_server.is_connected("charger_001")
        assert ocpp_server.is_connected("charger_002")
        assert ocpp_server.is_connected("charger_003")
        assert not ocpp_server.is_connected("charger_004")

    @pytest.mark.asyncio
    async def test_get_charge_point_by_id(self, ocpp_server):
        """Test retrieving charge point by ID."""
        mock_ws = AsyncMock()
        cp = FleetChargePoint(id="charger_001", connection=mock_ws)
        ocpp_server.charge_points["charger_001"] = cp

        retrieved = ocpp_server.get_charge_point("charger_001")
        assert retrieved is cp

        not_found = ocpp_server.get_charge_point("nonexistent")
        assert not_found is None

    @pytest.mark.asyncio
    async def test_charger_disconnect_cleanup(self, ocpp_server):
        """Test charge point cleanup on disconnect."""
        mock_ws = AsyncMock()
        mock_ws.close = AsyncMock()

        cp = FleetChargePoint(id="charger_001", connection=mock_ws)
        ocpp_server.charge_points["charger_001"] = cp

        assert ocpp_server.is_connected("charger_001")

        # Simulate disconnect
        del ocpp_server.charge_points["charger_001"]

        assert not ocpp_server.is_connected("charger_001")

    @pytest.mark.asyncio
    async def test_concurrent_charger_messages(self, ocpp_server):
        """Test handling concurrent messages from multiple chargers."""
        callbacks_received = []

        async def on_status(cp_id, connector_id, status):
            callbacks_received.append((cp_id, connector_id, status))

        ocpp_server.on_status_change = on_status

        # Create multiple charge points
        for i in range(3):
            mock_ws = AsyncMock()
            cp = FleetChargePoint(
                id=f"charger_{i:03d}",
                connection=mock_ws,
                on_status_change=on_status,
            )
            ocpp_server.charge_points[f"charger_{i:03d}"] = cp

            # Simulate status notification
            await cp.on_status_notification(
                connector_id=1,
                error_code="NoError",
                status="Available",
            )

        # All callbacks should have been received
        assert len(callbacks_received) == 3


# ============ Connection Timeout/Recovery Tests ============


class TestConnectionTimeoutRecovery:
    """Tests for connection timeout and recovery scenarios."""

    @pytest.mark.asyncio
    async def test_connection_error_handling(self, ocpp_server, mock_websocket):
        """Test handling of connection errors."""
        mock_websocket.close = AsyncMock()

        # Simulate connection error during message processing
        with patch.object(FleetChargePoint, "start", new_callable=AsyncMock) as mock_start:
            mock_start.side_effect = ConnectionError("Connection lost")

            await ocpp_server.on_connect(mock_websocket, "/charger_001")

            # Charge point should be cleaned up
            assert "charger_001" not in ocpp_server.charge_points

    @pytest.mark.asyncio
    async def test_set_charging_profile_timeout(self, charge_point):
        """Test SetChargingProfile timeout handling."""

        async def slow_call(*args, **kwargs):
            await asyncio.sleep(10)

        charge_point.call = slow_call

        schedule = [{"startPeriod": 0, "limit": 80000, "numberPhases": 3}]

        # Use asyncio.wait_for to enforce timeout
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                charge_point.set_charging_profile(1, schedule, max_retries=1), timeout=0.5
            )

    @pytest.mark.asyncio
    async def test_reconnection_after_disconnect(self, ocpp_server):
        """Test charge point reconnection after disconnect."""
        mock_ws1 = AsyncMock()
        mock_ws2 = AsyncMock()

        # First connection
        with patch.object(FleetChargePoint, "start", new_callable=AsyncMock) as mock_start:
            mock_start.side_effect = asyncio.CancelledError()

            try:
                await ocpp_server.on_connect(mock_ws1, "/charger_001")
            except asyncio.CancelledError:
                pass

        # Simulate reconnection with new WebSocket
        with patch.object(FleetChargePoint, "start", new_callable=AsyncMock) as mock_start:
            mock_start.side_effect = asyncio.CancelledError()

            try:
                await ocpp_server.on_connect(mock_ws2, "/charger_001")
            except asyncio.CancelledError:
                pass

        # Should have been cleaned up after disconnect
        assert "charger_001" not in ocpp_server.charge_points


# ============ Schedule Conversion Integration Tests ============


class TestScheduleConversionIntegration:
    """Tests for schedule conversion and dispatch integration."""

    def test_optimization_schedule_to_ocpp(self):
        """Test converting optimization schedule to OCPP format."""
        # Optimization output: timestep -> power
        opt_schedule = [
            (0, 80.0),
            (1, 80.0),
            (2, 60.0),
            (3, 40.0),
            (4, 0.0),
        ]

        ocpp_profile = convert_schedule_to_ocpp_profile(opt_schedule, delta_t=0.25)

        assert len(ocpp_profile) == 5

        # Verify startPeriod calculations (15-minute intervals)
        assert ocpp_profile[0]["startPeriod"] == 0
        assert ocpp_profile[1]["startPeriod"] == 900  # 15 minutes
        assert ocpp_profile[2]["startPeriod"] == 1800  # 30 minutes

        # Verify power conversion (kW to W)
        assert ocpp_profile[0]["limit"] == 80000
        assert ocpp_profile[2]["limit"] == 60000
        assert ocpp_profile[4]["limit"] == 0

    def test_24_hour_schedule_conversion(self):
        """Test converting full 24-hour schedule."""
        # 96 timesteps at 15-minute intervals
        opt_schedule = [(t, 80.0 if t < 48 else 0.0) for t in range(96)]

        ocpp_profile = convert_schedule_to_ocpp_profile(opt_schedule, delta_t=0.25)

        assert len(ocpp_profile) == 96

        # Last period starts at 95 * 15 minutes = 1425 minutes = 85500 seconds
        assert ocpp_profile[95]["startPeriod"] == 95 * 900

    @pytest.mark.asyncio
    async def test_dispatch_converted_schedule(self, charge_point):
        """Test dispatching converted optimization schedule."""
        mock_response = MagicMock()
        mock_response.status = "Accepted"
        charge_point.call = AsyncMock(return_value=mock_response)

        # Convert and dispatch
        opt_schedule = [(t, 80.0 - t * 2) for t in range(16)]  # 4 hours
        ocpp_profile = convert_schedule_to_ocpp_profile(opt_schedule)

        result = await charge_point.set_charging_profile(1, ocpp_profile)

        assert result is True


# ============ Error Code Handling Tests ============


class TestOCPPErrorHandling:
    """Tests for OCPP error code handling."""

    @pytest.mark.asyncio
    async def test_status_notification_with_error(self, charge_point):
        """Test StatusNotification with error code."""
        response = await charge_point.on_status_notification(
            connector_id=1,
            error_code="GroundFailure",
            status="Faulted",
        )

        assert response is not None

        # Callback should receive the status
        args = charge_point.on_status_change.call_args[0]
        assert args[2] == "Faulted"

    @pytest.mark.asyncio
    async def test_various_error_codes(self, charge_point):
        """Test handling of various OCPP error codes."""
        error_codes = [
            "ConnectorLockFailure",
            "EVCommunicationError",
            "GroundFailure",
            "HighTemperature",
            "InternalError",
            "LocalListConflict",
            "NoError",
            "OtherError",
            "OverCurrentFailure",
            "PowerMeterFailure",
            "PowerSwitchFailure",
            "ReaderFailure",
            "ResetFailure",
            "UnderVoltage",
            "OverVoltage",
            "WeakSignal",
        ]

        for error_code in error_codes:
            charge_point.on_status_change.reset_mock()

            response = await charge_point.on_status_notification(
                connector_id=1,
                error_code=error_code,
                status="Available" if error_code == "NoError" else "Faulted",
            )

            assert response is not None
