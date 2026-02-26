"""Unit tests for DER Control Manager module - minimal working version."""

from unittest.mock import AsyncMock, patch

import pytest

from src.websocket_handler.der_control_manager import (
    DERControlEnumType,
    DERControlManager,
    DERControlType,
    DERCurve,
    DERCurvePoint,
    DERCurveType,
)


class TestDERControlEnumType:
    """Test DERControlEnumType enum."""

    def test_enum_values(self):
        """Test DER control enum values."""
        assert DERControlEnumType.FIXED_PF_INJECT.value == "FixedPFInject"
        assert DERControlEnumType.FIXED_PF_ABSORB.value == "FixedPFAbsorb"
        assert DERControlEnumType.VOLT_VAR.value == "VoltVar"
        assert DERControlEnumType.WATT_VAR.value == "WattVar"
        assert DERControlEnumType.FIXED_VAR.value == "FixedVar"
        assert DERControlEnumType.VOLT_WATT.value == "VoltWatt"
        assert DERControlEnumType.FREQ_DROOP.value == "FreqDroop"
        assert DERControlEnumType.LIMIT_MAX_DISCHARGE.value == "LimitMaxDischarge"
        assert DERControlEnumType.LIMIT_MAX_CHARGE.value == "LimitMaxCharge"
        assert DERControlEnumType.LIMIT_VAR.value == "LimitVar"
        assert DERControlEnumType.LIMIT_WATT.value == "LimitWatt"


class TestDERCurveType:
    """Test DERCurveType enum."""

    def test_enum_values(self):
        """Test DER curve enum values."""
        assert DERCurveType.FREQ_DROOP.value == "FreqDroop"
        assert DERCurveType.VOLT_VAR.value == "VoltVar"
        assert DERCurveType.WATT_VAR.value == "WattVar"
        assert DERCurveType.VOLT_WATT.value == "VoltWatt"


class TestDERCurvePoint:
    """Test DERCurvePoint class."""

    def test_curve_point_creation(self):
        """Test DERCurvePoint creation."""
        point = DERCurvePoint(x=50.0, y=1000.0)

        assert point.x == 50.0
        assert point.y == 1000.0


class TestDERCurve:
    """Test DERCurve class."""

    def test_curve_creation(self):
        """Test DERCurve creation."""
        points = [DERCurvePoint(x=50.0, y=1000.0), DERCurvePoint(x=60.0, y=2000.0)]
        curve = DERCurve(
            curve_type=DERCurveType.VOLT_VAR, points=points, curve_unit_x="V", curve_unit_y="VAR"
        )

        assert curve.curve_type == DERCurveType.VOLT_VAR
        assert len(curve.points) == 2
        assert curve.points[0].x == 50.0
        assert curve.points[1].x == 60.0
        assert curve.curve_unit_x == "V"
        assert curve.curve_unit_y == "VAR"


class TestDERControlType:
    """Test DERControlType class."""

    def test_der_control_type_creation(self):
        """Test DERControlType creation."""
        control_type = DERControlType(
            control_id=1, control_type=DERControlEnumType.VOLT_VAR, priority=1
        )

        assert control_type.control_id == 1
        assert control_type.control_type == DERControlEnumType.VOLT_VAR
        assert control_type.priority == 1
        assert control_type.is_default is False
        assert control_type.is_superseded is False
        assert control_type.curve is None


class TestDERControlManager:
    """Test DERControlManager class."""

    @pytest.fixture
    def mock_timescale_client(self):
        """Mock TimescaleDB client."""
        client = AsyncMock()
        client.insert_data = AsyncMock()
        client.query_data = AsyncMock()
        return client

    @pytest.fixture
    def der_control_manager(self, mock_timescale_client):
        """Create DERControlManager instance."""
        return DERControlManager(timescale_client=mock_timescale_client)

    @pytest.mark.timeout(10)
    def test_der_control_manager_initialization(self, mock_timescale_client):
        """Test DERControlManager initialization."""
        manager = DERControlManager(timescale_client=mock_timescale_client)

        assert manager.timescale_client == mock_timescale_client
        assert manager.active_controls == {}
        assert manager.control_id_counters == {}

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_set_der_control(self, der_control_manager):
        """Test setting DER control."""
        station_id = "TEST_STATION_001"
        control_data = {"control_type": "VoltVar", "priority": 1}

        with (
            patch.object(der_control_manager, "_parse_der_control") as mock_parse,
            patch.object(
                der_control_manager, "_validate_der_control", return_value={"valid": True}
            ),
            patch.object(der_control_manager, "_handle_control_priority", return_value=None),
            patch.object(der_control_manager, "_get_next_control_id", return_value=1),
            patch.object(der_control_manager, "_store_der_control", return_value=None),
            patch.object(der_control_manager, "_update_control_cache", return_value=None),
        ):

            mock_control = DERControlType(
                control_id=1, control_type=DERControlEnumType.VOLT_VAR, priority=1
            )
            mock_parse.return_value = mock_control

            response = await der_control_manager.set_der_control(station_id, control_data)

            assert response is not None
            assert response["status"] == "Accepted"
            assert "statusInfo" in response

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_set_der_control_invalid(self, der_control_manager):
        """Test setting invalid DER control."""
        station_id = "TEST_STATION_001"
        control_data = {"control_type": "InvalidType", "priority": 1}

        with (
            patch.object(der_control_manager, "_parse_der_control") as mock_parse,
            patch.object(
                der_control_manager,
                "_validate_der_control",
                return_value={
                    "valid": False,
                    "reason_code": "InvalidType",
                    "message": "Invalid control type",
                },
            ),
        ):

            mock_control = DERControlType(
                control_id=1, control_type=DERControlEnumType.FIXED_PF_INJECT, priority=1
            )
            mock_parse.return_value = mock_control

            response = await der_control_manager.set_der_control(station_id, control_data)

            assert response is not None
            assert response["status"] == "Rejected"
            assert response["statusInfo"]["reasonCode"] == "InvalidType"

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_get_der_control_not_found(self, der_control_manager):
        """Test getting non-existent DER control."""
        station_id = "TEST_STATION_001"
        control_id = 999

        response = await der_control_manager.get_der_control(station_id, control_id)

        assert response is not None
        assert response["status"] == "Rejected"
        assert response["statusInfo"]["reasonCode"] == "NotFound"

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_clear_der_control_not_found(self, der_control_manager):
        """Test clearing non-existent DER control."""
        station_id = "TEST_STATION_001"
        control_id = 999

        response = await der_control_manager.clear_der_control(station_id, control_id)

        assert response is not None
        assert response["status"] == "Rejected"

    @pytest.mark.timeout(10)
    def test_der_control_manager_without_client(self):
        """Test DERControlManager initialization without client."""
        manager = DERControlManager(timescale_client=None)

        assert manager.timescale_client is None
        assert manager.active_controls == {}
        assert manager.control_id_counters == {}
