"""Comprehensive unit tests for OCPP server and charge point functionality.

Reference: Development plan Step 3.1, PRD.md#9-1-ocpp-integration
Tests: WebSocket handshake, message routing, handlers, connection lifecycle
"""

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.adapters.ocpp.charge_point import (
    FleetChargePoint,
    convert_schedule_to_ocpp_profile,
)

# OCPP imports
from src.adapters.ocpp.server import OCPPServer

# ============ OCPPServer Initialization Tests ============


class TestOCPPServerInit:
    """Tests for OCPPServer initialization."""

    def test_server_default_init(self):
        """Test server initializes with default values."""
        server = OCPPServer()
        assert server.host == "0.0.0.0"
        assert server.port == 9000
        assert server.pools is None
        assert server.charge_points == {}
        assert server.on_status_change is None
        assert server.on_meter_values is None
        assert server._running is False

    def test_server_custom_init(self):
        """Test server initializes with custom values."""
        mock_pool = MagicMock()
        mock_status_cb = MagicMock()
        mock_meter_cb = MagicMock()

        server = OCPPServer(
            host="127.0.0.1",
            port=9001,
            pools=mock_pool,
            on_status_change=mock_status_cb,
            on_meter_values=mock_meter_cb,
        )

        assert server.host == "127.0.0.1"
        assert server.port == 9001
        assert server.pools is mock_pool
        assert server.on_status_change is mock_status_cb
        assert server.on_meter_values is mock_meter_cb


# ============ Connection Handling Tests ============


class TestOCPPServerConnection:
    """Tests for connection handling."""

    @pytest.fixture
    def server(self):
        """Create a basic server instance."""
        return OCPPServer()

    @pytest.fixture
    def mock_websocket(self):
        """Create a mock WebSocket connection."""
        ws = AsyncMock()
        ws.close = AsyncMock()
        return ws

    @pytest.mark.asyncio
    async def test_on_connect_valid_path(self, server, mock_websocket):
        """Test connection with valid charge point ID path."""
        with patch.object(FleetChargePoint, "start", new_callable=AsyncMock) as mock_start:
            mock_start.side_effect = asyncio.CancelledError()  # Simulate stop

            try:
                await server.on_connect(mock_websocket, "/charger_001")
            except asyncio.CancelledError:
                pass

            assert "charger_001" not in server.charge_points  # Cleaned up after disconnect

    @pytest.mark.asyncio
    async def test_on_connect_empty_path(self, server, mock_websocket):
        """Test connection with empty path closes WebSocket."""
        await server.on_connect(mock_websocket, "/")
        mock_websocket.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_connect_invalid_path(self, server, mock_websocket):
        """Test connection with only slashes closes WebSocket."""
        await server.on_connect(mock_websocket, "///")
        # Empty after stripping slashes
        mock_websocket.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_connect_cleans_up_on_error(self, server, mock_websocket):
        """Test charge point cleanup on connection error."""
        with patch.object(FleetChargePoint, "start", new_callable=AsyncMock) as mock_start:
            mock_start.side_effect = Exception("Connection error")

            await server.on_connect(mock_websocket, "/charger_001")

            assert "charger_001" not in server.charge_points

    @pytest.mark.asyncio
    async def test_on_connect_cleans_up_on_websocket_close(self, server, mock_websocket):
        """Test charge point cleanup on WebSocket close."""
        import websockets.exceptions

        with patch.object(FleetChargePoint, "start", new_callable=AsyncMock) as mock_start:
            mock_start.side_effect = websockets.exceptions.ConnectionClosed(None, None)

            await server.on_connect(mock_websocket, "/charger_001")

            assert "charger_001" not in server.charge_points

    @pytest.mark.asyncio
    async def test_multiple_charge_points_connect(self, server):
        """Test multiple charge points can connect."""
        mock_ws1 = AsyncMock()
        mock_ws2 = AsyncMock()

        with patch.object(FleetChargePoint, "start", new_callable=AsyncMock) as mock_start:
            # First connection succeeds then disconnects
            call_count = [0]

            async def controlled_disconnect(*args):
                call_count[0] += 1
                if call_count[0] == 1:
                    # Let first register before exception
                    await asyncio.sleep(0.01)
                raise asyncio.CancelledError()

            mock_start.side_effect = controlled_disconnect

            # Start both connections
            tasks = [
                asyncio.create_task(server.on_connect(mock_ws1, "/charger_001")),
                asyncio.create_task(server.on_connect(mock_ws2, "/charger_002")),
            ]

            await asyncio.sleep(0.02)  # Let connections register

            for task in tasks:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass


# ============ Callback Tests ============


class TestOCPPServerCallbacks:
    """Tests for server callbacks."""

    @pytest.fixture
    def server_with_callbacks(self):
        """Create server with mock callbacks."""
        on_status = AsyncMock()
        on_meter = AsyncMock()
        return (
            OCPPServer(
                on_status_change=on_status,
                on_meter_values=on_meter,
            ),
            on_status,
            on_meter,
        )

    @pytest.mark.asyncio
    async def test_handle_status_change_calls_callback(self, server_with_callbacks):
        """Test status change handler calls user callback."""
        server, on_status, _ = server_with_callbacks

        await server._handle_status_change("charger_001", 1, "Charging")

        on_status.assert_called_once_with("charger_001", 1, "Charging")

    @pytest.mark.asyncio
    async def test_handle_status_change_callback_error(self, server_with_callbacks):
        """Test status change handles callback errors gracefully."""
        server, on_status, _ = server_with_callbacks
        on_status.side_effect = Exception("Callback error")

        # Should not raise
        await server._handle_status_change("charger_001", 1, "Charging")

    @pytest.mark.asyncio
    async def test_handle_status_change_stores_to_db(self):
        """Test status change stores to database when pool available."""
        mock_pool = MagicMock()
        mock_conn = AsyncMock()
        mock_pool.acquire.return_value.__aenter__.return_value = mock_conn

        server = OCPPServer(pools=mock_pool)

        with patch.object(server, "_store_status_update", new_callable=AsyncMock) as mock_store:
            await server._handle_status_change("charger_001", 1, "Charging")
            mock_store.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_meter_values_calls_callback(self, server_with_callbacks):
        """Test meter values handler calls user callback."""
        server, _, on_meter = server_with_callbacks
        ts = datetime.now(timezone.utc)

        await server._handle_meter_values("charger_001", 1, 0.75, 50.0, ts)

        on_meter.assert_called_once_with("charger_001", 1, 0.75, 50.0, ts, None)

    @pytest.mark.asyncio
    async def test_handle_meter_values_callback_error(self, server_with_callbacks):
        """Test meter values handles callback errors gracefully."""
        server, _, on_meter = server_with_callbacks
        on_meter.side_effect = Exception("Callback error")
        ts = datetime.now(timezone.utc)

        # Should not raise
        await server._handle_meter_values("charger_001", 1, 0.75, 50.0, ts)

    @pytest.mark.asyncio
    async def test_handle_meter_values_stores_to_db(self):
        """Test meter values stores to database when pool available."""
        mock_pool = MagicMock()
        server = OCPPServer(pools=mock_pool)
        ts = datetime.now(timezone.utc)

        with patch.object(server, "_store_meter_values", new_callable=AsyncMock) as mock_store:
            await server._handle_meter_values("charger_001", 1, 0.75, 50.0, ts)
            mock_store.assert_called_once()


# ============ Charge Point Registry Tests ============


class TestChargePointRegistry:
    """Tests for charge point registry management."""

    @pytest.fixture
    def server(self):
        """Create a basic server instance."""
        return OCPPServer()

    def test_get_charge_point_not_connected(self, server):
        """Test get_charge_point returns None for unconnected charger."""
        assert server.get_charge_point("unknown_charger") is None

    def test_get_charge_point_connected(self, server):
        """Test get_charge_point returns charge point when connected."""
        mock_cp = MagicMock()
        server.charge_points["charger_001"] = mock_cp

        result = server.get_charge_point("charger_001")
        assert result is mock_cp

    def test_is_connected_false(self, server):
        """Test is_connected returns False for unconnected charger."""
        assert server.is_connected("unknown_charger") is False

    def test_is_connected_true(self, server):
        """Test is_connected returns True for connected charger."""
        server.charge_points["charger_001"] = MagicMock()
        assert server.is_connected("charger_001") is True


# ============ Server Lifecycle Tests ============


class TestOCPPServerLifecycle:
    """Tests for server start/stop lifecycle."""

    @pytest.fixture
    def server(self):
        """Create a basic server instance."""
        return OCPPServer()

    @pytest.mark.asyncio
    async def test_start_when_already_running(self, server):
        """Test start does nothing when server already running."""
        server._running = True

        with patch("websockets.serve") as mock_serve:
            await server.start()
            mock_serve.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_closes_all_connections(self, server):
        """Test stop closes all charge point connections."""
        mock_cp1 = AsyncMock()
        mock_cp2 = AsyncMock()
        server.charge_points = {"cp1": mock_cp1, "cp2": mock_cp2}
        server.server = MagicMock()
        server.server.close = MagicMock()
        server.server.wait_closed = AsyncMock()

        await server.stop()

        mock_cp1.close.assert_called_once()
        mock_cp2.close.assert_called_once()
        assert server.charge_points == {}
        assert server._running is False

    @pytest.mark.asyncio
    async def test_stop_handles_close_errors(self, server):
        """Test stop handles errors when closing charge points."""
        mock_cp = AsyncMock()
        mock_cp.close.side_effect = Exception("Close error")
        server.charge_points = {"cp1": mock_cp}
        server.server = MagicMock()
        server.server.close = MagicMock()
        server.server.wait_closed = AsyncMock()

        # Should not raise
        await server.stop()
        assert server.charge_points == {}


# ============ FleetChargePoint Handler Tests ============


class TestFleetChargePointHandlers:
    """Tests for FleetChargePoint OCPP message handlers."""

    @pytest.fixture
    def charge_point(self):
        """Create a FleetChargePoint instance."""
        mock_ws = AsyncMock()
        return FleetChargePoint(
            id="test_charger",
            connection=mock_ws,
            on_status_change=AsyncMock(),
            on_meter_values=AsyncMock(),
        )

    @pytest.mark.asyncio
    async def test_boot_notification_response(self, charge_point):
        """Test BootNotification returns Accepted response."""
        response = await charge_point.on_boot_notification(
            charge_point_vendor="TestVendor",
            charge_point_model="TestModel",
        )

        assert response.status == "Accepted"
        assert response.interval == 300

    @pytest.mark.asyncio
    async def test_boot_notification_with_extra_fields(self, charge_point):
        """Test BootNotification handles extra fields."""
        response = await charge_point.on_boot_notification(
            charge_point_vendor="TestVendor",
            charge_point_model="TestModel",
            serial_number="SN123456",
            firmware_version="1.0.0",
        )

        assert response.status == "Accepted"

    @pytest.mark.asyncio
    async def test_status_notification_calls_callback(self, charge_point):
        """Test StatusNotification calls status change callback."""
        await charge_point.on_status_notification(
            connector_id=1,
            error_code="NoError",
            status="Available",
        )

        charge_point.on_status_change.assert_called_once_with("test_charger", 1, "Available", "NoError", None, None, None)

    @pytest.mark.asyncio
    async def test_status_notification_callback_error(self, charge_point):
        """Test StatusNotification handles callback errors."""
        charge_point.on_status_change.side_effect = Exception("Callback error")

        # Should not raise
        response = await charge_point.on_status_notification(
            connector_id=1,
            error_code="NoError",
            status="Charging",
        )

        assert response is not None

    @pytest.mark.asyncio
    async def test_meter_values_extracts_soc(self, charge_point):
        """Test MeterValues extracts SoC correctly."""
        ts = datetime.now(timezone.utc).isoformat()
        meter_value = [
            {
                "timestamp": ts,
                "sampledValue": [
                    {"measurand": "SoC", "value": "75", "unit": "%"},
                ],
            }
        ]

        await charge_point.on_meter_values(connector_id=1, meter_value=meter_value)

        # Callback should be called with SoC converted to 0-1 scale
        charge_point.on_meter_values_callback.assert_called_once()
        args = charge_point.on_meter_values_callback.call_args[0]
        assert args[2] == 0.75  # SoC

    @pytest.mark.asyncio
    async def test_meter_values_extracts_power_watts(self, charge_point):
        """Test MeterValues extracts power in Watts."""
        ts = datetime.now(timezone.utc).isoformat()
        meter_value = [
            {
                "timestamp": ts,
                "sampledValue": [
                    {"measurand": "Power.Active.Import", "value": "50000", "unit": "W"},
                ],
            }
        ]

        await charge_point.on_meter_values(connector_id=1, meter_value=meter_value)

        args = charge_point.on_meter_values_callback.call_args[0]
        assert args[3] == 50.0  # Power in kW

    @pytest.mark.asyncio
    async def test_meter_values_extracts_power_kw(self, charge_point):
        """Test MeterValues extracts power already in kW."""
        ts = datetime.now(timezone.utc).isoformat()
        meter_value = [
            {
                "timestamp": ts,
                "sampledValue": [
                    {"measurand": "Power.Active.Import", "value": "50", "unit": "kW"},
                ],
            }
        ]

        await charge_point.on_meter_values(connector_id=1, meter_value=meter_value)

        args = charge_point.on_meter_values_callback.call_args[0]
        assert args[3] == 50.0  # Power in kW

    @pytest.mark.asyncio
    async def test_meter_values_handles_invalid_value(self, charge_point):
        """Test MeterValues handles invalid meter values."""
        ts = datetime.now(timezone.utc).isoformat()
        meter_value = [
            {
                "timestamp": ts,
                "sampledValue": [
                    {"measurand": "SoC", "value": "invalid", "unit": "%"},
                ],
            }
        ]

        # Should not raise
        await charge_point.on_meter_values(connector_id=1, meter_value=meter_value)

    @pytest.mark.asyncio
    async def test_meter_values_datetime_object_timestamp(self, charge_point):
        """Test MeterValues handles datetime object timestamp."""
        ts = datetime.now(timezone.utc)
        meter_value = [
            {
                "timestamp": ts,
                "sampledValue": [
                    {"measurand": "SoC", "value": "50", "unit": "%"},
                ],
            }
        ]

        await charge_point.on_meter_values(connector_id=1, meter_value=meter_value)

        charge_point.on_meter_values_callback.assert_called_once()

    @pytest.mark.asyncio
    async def test_start_transaction(self, charge_point):
        """Test StartTransaction returns transaction ID."""
        response = await charge_point.on_start_transaction(
            connector_id=1,
            id_tag="USER123",
            meter_start=0,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        assert response.transaction_id is not None
        assert response.id_tag_info["status"] == "Accepted"
        assert charge_point.current_transaction_id is not None

    @pytest.mark.asyncio
    async def test_stop_transaction(self, charge_point):
        """Test StopTransaction clears transaction ID."""
        # First start a transaction
        start_response = await charge_point.on_start_transaction(
            connector_id=1,
            id_tag="USER123",
            meter_start=0,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        transaction_id = start_response.transaction_id

        # Then stop it
        stop_response = await charge_point.on_stop_transaction(
            transaction_id=transaction_id,
            id_tag="USER123",
            meter_stop=100,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        assert stop_response.id_tag_info["status"] == "Accepted"
        assert charge_point.current_transaction_id is None


# ============ SetChargingProfile Tests ============


class TestSetChargingProfile:
    """Tests for SetChargingProfile command."""

    @pytest.fixture
    def charge_point(self):
        """Create a FleetChargePoint instance."""
        mock_ws = AsyncMock()
        return FleetChargePoint(id="test_charger", connection=mock_ws)

    @pytest.mark.asyncio
    async def test_set_charging_profile_accepted(self, charge_point):
        """Test SetChargingProfile returns True when accepted."""
        mock_response = MagicMock()
        mock_response.status = "Accepted"
        charge_point.call = AsyncMock(return_value=mock_response)

        schedule = [{"startPeriod": 0, "limit": 80000, "numberPhases": 3}]
        result = await charge_point.set_charging_profile(1, schedule)

        assert result is True
        charge_point.call.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_charging_profile_rejected(self, charge_point):
        """Test SetChargingProfile returns False when rejected."""
        mock_response = MagicMock()
        mock_response.status = "Rejected"
        charge_point.call = AsyncMock(return_value=mock_response)

        schedule = [{"startPeriod": 0, "limit": 80000, "numberPhases": 3}]
        result = await charge_point.set_charging_profile(1, schedule, max_retries=1)

        assert result is False

    @pytest.mark.asyncio
    async def test_set_charging_profile_retries_on_failure(self, charge_point):
        """Test SetChargingProfile retries on failure."""
        mock_response = MagicMock()
        mock_response.status = "Rejected"
        charge_point.call = AsyncMock(return_value=mock_response)

        schedule = [{"startPeriod": 0, "limit": 80000, "numberPhases": 3}]

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await charge_point.set_charging_profile(1, schedule, max_retries=3)

        assert result is False
        assert charge_point.call.call_count == 3

    @pytest.mark.asyncio
    async def test_set_charging_profile_retries_on_exception(self, charge_point):
        """Test SetChargingProfile retries on exception."""
        charge_point.call = AsyncMock(side_effect=Exception("Network error"))

        schedule = [{"startPeriod": 0, "limit": 80000, "numberPhases": 3}]

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await charge_point.set_charging_profile(1, schedule, max_retries=2)

        assert result is False
        assert charge_point.call.call_count == 2

    @pytest.mark.asyncio
    async def test_set_charging_profile_success_after_retry(self, charge_point):
        """Test SetChargingProfile succeeds after retry."""
        reject_response = MagicMock()
        reject_response.status = "Rejected"
        accept_response = MagicMock()
        accept_response.status = "Accepted"

        charge_point.call = AsyncMock(side_effect=[reject_response, accept_response])

        schedule = [{"startPeriod": 0, "limit": 80000, "numberPhases": 3}]

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await charge_point.set_charging_profile(1, schedule, max_retries=3)

        assert result is True
        assert charge_point.call.call_count == 2


# ============ Remote Transaction Tests ============


class TestRemoteTransactions:
    """Tests for remote transaction commands."""

    @pytest.fixture
    def charge_point(self):
        """Create a FleetChargePoint instance."""
        mock_ws = AsyncMock()
        return FleetChargePoint(id="test_charger", connection=mock_ws)

    @pytest.mark.asyncio
    async def test_remote_start_accepted(self, charge_point):
        """Test RemoteStartTransaction returns True when accepted."""
        mock_response = MagicMock()
        mock_response.status = "Accepted"
        charge_point.call = AsyncMock(return_value=mock_response)

        result = await charge_point.remote_start_transaction(1, "USER123")

        assert result is True

    @pytest.mark.asyncio
    async def test_remote_start_rejected(self, charge_point):
        """Test RemoteStartTransaction returns False when rejected."""
        mock_response = MagicMock()
        mock_response.status = "Rejected"
        charge_point.call = AsyncMock(return_value=mock_response)

        result = await charge_point.remote_start_transaction(1, "USER123")

        assert result is False

    @pytest.mark.asyncio
    async def test_remote_start_exception(self, charge_point):
        """Test RemoteStartTransaction handles exceptions."""
        charge_point.call = AsyncMock(side_effect=Exception("Network error"))

        result = await charge_point.remote_start_transaction(1, "USER123")

        assert result is False

    @pytest.mark.asyncio
    async def test_remote_stop_accepted(self, charge_point):
        """Test RemoteStopTransaction returns True when accepted."""
        mock_response = MagicMock()
        mock_response.status = "Accepted"
        charge_point.call = AsyncMock(return_value=mock_response)

        result = await charge_point.remote_stop_transaction(12345)

        assert result is True

    @pytest.mark.asyncio
    async def test_remote_stop_rejected(self, charge_point):
        """Test RemoteStopTransaction returns False when rejected."""
        mock_response = MagicMock()
        mock_response.status = "Rejected"
        charge_point.call = AsyncMock(return_value=mock_response)

        result = await charge_point.remote_stop_transaction(12345)

        assert result is False

    @pytest.mark.asyncio
    async def test_remote_stop_exception(self, charge_point):
        """Test RemoteStopTransaction handles exceptions."""
        charge_point.call = AsyncMock(side_effect=Exception("Network error"))

        result = await charge_point.remote_stop_transaction(12345)

        assert result is False


# ============ Schedule Conversion Tests ============


class TestScheduleConversion:
    """Tests for convert_schedule_to_ocpp_profile."""

    def test_basic_conversion(self):
        """Test basic schedule conversion."""
        schedule = [(0, 80.0), (1, 60.0), (2, 40.0)]
        profile = convert_schedule_to_ocpp_profile(schedule)

        assert len(profile) == 3
        assert profile[0] == {"startPeriod": 0, "limit": 80000, "numberPhases": 3}
        assert profile[1] == {"startPeriod": 900, "limit": 60000, "numberPhases": 3}
        assert profile[2] == {"startPeriod": 1800, "limit": 40000, "numberPhases": 3}

    def test_custom_delta_t(self):
        """Test conversion with custom time step."""
        schedule = [(0, 80.0), (1, 60.0)]
        profile = convert_schedule_to_ocpp_profile(schedule, delta_t=0.5)

        assert profile[0]["startPeriod"] == 0
        assert profile[1]["startPeriod"] == 1800  # 30 minutes in seconds

    def test_custom_number_phases(self):
        """Test conversion with custom number of phases."""
        schedule = [(0, 80.0)]
        profile = convert_schedule_to_ocpp_profile(schedule, number_phases=1)

        assert profile[0]["numberPhases"] == 1

    def test_empty_schedule(self):
        """Test conversion of empty schedule."""
        profile = convert_schedule_to_ocpp_profile([])
        assert profile == []

    def test_zero_power(self):
        """Test conversion with zero power."""
        schedule = [(0, 0.0), (1, 80.0), (2, 0.0)]
        profile = convert_schedule_to_ocpp_profile(schedule)

        assert profile[0]["limit"] == 0
        assert profile[1]["limit"] == 80000
        assert profile[2]["limit"] == 0

    def test_fractional_power(self):
        """Test conversion with fractional power values."""
        schedule = [(0, 80.5)]
        profile = convert_schedule_to_ocpp_profile(schedule)

        assert profile[0]["limit"] == 80500

    def test_large_timestep(self):
        """Test conversion with large timestep."""
        schedule = [(0, 80.0), (100, 60.0)]
        profile = convert_schedule_to_ocpp_profile(schedule, delta_t=0.25)

        assert profile[1]["startPeriod"] == 100 * 0.25 * 3600  # 25 hours in seconds


# ============ Database Storage Tests ============


class TestDatabaseStorage:
    """Tests for database storage methods."""

    @pytest.fixture
    def server_with_pool(self):
        """Create server with mock database pool."""
        mock_pool = MagicMock()
        mock_conn = AsyncMock()
        mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
        mock_pool.acquire.return_value.__aexit__.return_value = None
        return OCPPServer(pools=mock_pool), mock_pool, mock_conn

    @pytest.mark.asyncio
    async def test_store_meter_values_success(self, server_with_pool):
        """Test successful meter values storage."""
        server, _, mock_conn = server_with_pool
        ts = datetime.now(timezone.utc)

        with (
            patch("src.adapters.ocpp.server.get_charger_id_from_ocpp_id", return_value=None),
            patch(
                "src.adapters.ocpp.telemetry.store_meter_values", new_callable=AsyncMock
            ) as mock_store,
        ):
            await server._store_meter_values("charger_001", 1, 0.75, 50.0, None, ts)

        mock_store.assert_called_once()

    @pytest.mark.asyncio
    async def test_store_meter_values_db_error(self, server_with_pool):
        """Test meter values storage handles database errors."""
        server, _, mock_conn = server_with_pool
        ts = datetime.now(timezone.utc)

        with (
            patch("src.adapters.ocpp.server.get_charger_id_from_ocpp_id", return_value=None),
            patch(
                "src.adapters.ocpp.telemetry.store_meter_values",
                new_callable=AsyncMock,
                side_effect=Exception("DB error"),
            ),
        ):
            with pytest.raises(Exception, match="DB error"):
                await server._store_meter_values("charger_001", 1, 0.75, 50.0, None, ts)

    @pytest.mark.asyncio
    async def test_store_meter_values_no_pool(self):
        """Test meter values storage does nothing without pool."""
        server = OCPPServer()  # No pool
        ts = datetime.now(timezone.utc)

        # Should not raise
        await server._store_meter_values("charger_001", 1, 0.75, 50.0, None, ts)


# ============ Edge Cases Tests ============


class TestEdgeCases:
    """Edge case tests for OCPP components."""

    def test_charge_point_init_no_callbacks(self):
        """Test charge point initializes without callbacks."""
        mock_ws = AsyncMock()
        cp = FleetChargePoint(id="test", connection=mock_ws)

        assert cp.on_status_change is None
        assert cp.on_meter_values_callback is None

    @pytest.mark.asyncio
    async def test_meter_values_no_timestamp(self):
        """Test MeterValues handles missing timestamp."""
        mock_ws = AsyncMock()
        cp = FleetChargePoint(
            id="test",
            connection=mock_ws,
            on_meter_values=AsyncMock(),
        )

        meter_value = [
            {
                "sampledValue": [
                    {"measurand": "SoC", "value": "50", "unit": "%"},
                ]
            }
        ]

        # Should not raise, but callback not called without timestamp
        await cp.on_meter_values(connector_id=1, meter_value=meter_value)
        cp.on_meter_values_callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_meter_values_multiple_samples(self):
        """Test MeterValues handles multiple sampled values."""
        mock_ws = AsyncMock()
        on_meter = AsyncMock()
        cp = FleetChargePoint(
            id="test",
            connection=mock_ws,
            on_meter_values=on_meter,
        )

        ts = datetime.now(timezone.utc).isoformat()
        meter_value = [
            {
                "timestamp": ts,
                "sampledValue": [
                    {"measurand": "SoC", "value": "75", "unit": "%"},
                    {"measurand": "Power.Active.Import", "value": "50000", "unit": "W"},
                    {"measurand": "Energy.Active.Import.Register", "value": "100", "unit": "Wh"},
                ],
            }
        ]

        await cp.on_meter_values(connector_id=1, meter_value=meter_value)

        args = on_meter.call_args[0]
        assert args[2] == 0.75  # SoC
        assert args[3] == 50.0  # Power

    @pytest.mark.asyncio
    async def test_meter_values_z_timestamp(self):
        """Test MeterValues handles Z-suffix timestamp."""
        mock_ws = AsyncMock()
        on_meter = AsyncMock()
        cp = FleetChargePoint(
            id="test",
            connection=mock_ws,
            on_meter_values=on_meter,
        )

        meter_value = [
            {
                "timestamp": "2024-01-15T10:30:00Z",
                "sampledValue": [
                    {"measurand": "SoC", "value": "50", "unit": "%"},
                ],
            }
        ]

        await cp.on_meter_values(connector_id=1, meter_value=meter_value)

        on_meter.assert_called_once()

    @pytest.mark.asyncio
    async def test_status_notification_various_statuses(self):
        """Test StatusNotification handles various status values."""
        mock_ws = AsyncMock()
        on_status = AsyncMock()
        cp = FleetChargePoint(
            id="test",
            connection=mock_ws,
            on_status_change=on_status,
        )

        statuses = ["Available", "Charging", "Faulted", "Finishing", "Reserved", "Unavailable"]

        for status in statuses:
            await cp.on_status_notification(
                connector_id=1,
                error_code="NoError",
                status=status,
            )

        assert on_status.call_count == len(statuses)
