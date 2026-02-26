"""Tariff and Cost Management for OCPP 2.0.1."""

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional

from ocpp.v201.datatypes import CostType, SalesTariffEntryType, SalesTariffType, StatusInfoType
from ocpp.v201.enums import GenericStatusEnumType

from .monitoring import get_logger
from .timescale_client import TimescaleClient


class TariffElementType(Enum):
    """Tariff element types."""

    ENERGY = "Energy"
    TIME = "Time"
    PARKING = "Parking"
    POWER = "Power"


class TariffManager:
    """Manages tariffs and cost calculations for charging stations."""

    def __init__(self, timescale_client: TimescaleClient):
        """Initialize TariffManager."""
        self.timescale_client = timescale_client
        self.logger = get_logger(__name__)

    async def set_tariff(self, station_id: str, tariff_data: Dict[str, Any]) -> Dict[str, Any]:
        """Set tariff for station."""
        self.logger.info(f"Setting tariff for {station_id}")

        try:
            # Validate tariff data
            if not self._validate_tariff_data(tariff_data):
                return {
                    "status": GenericStatusEnumType.rejected,
                    "statusInfo": StatusInfoType(
                        reason_code="InvalidTariff", additional_info="Tariff validation failed"
                    ),
                }

            # Store tariff
            tariff_id = tariff_data.get("tariff_id") or str(uuid.uuid4())

            tariff_record = {
                "tariff_id": tariff_id,
                "station_id": station_id,
                "tariff_description": tariff_data.get("description"),
                "tariff_currency": tariff_data.get("currency", "USD"),
                "tariff_priority": tariff_data.get("priority", 0),
                "valid_from": tariff_data.get("valid_from"),
                "valid_to": tariff_data.get("valid_to"),
                "tariff_data": tariff_data,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }

            await self.timescale_client.store_tariff(tariff_record)

            # Store tariff elements
            if "elements" in tariff_data:
                for element in tariff_data["elements"]:
                    element_id = element.get("element_id") or str(uuid.uuid4())
                    element_record = {
                        "element_id": element_id,
                        "tariff_id": tariff_id,
                        "element_type": element["type"],
                        "price_per_unit": Decimal(str(element["price_per_unit"])),
                        "currency": element.get("currency", "USD"),
                        "unit": element["unit"],
                        "valid_from": element.get("valid_from"),
                        "valid_to": element.get("valid_to"),
                        "created_at": datetime.now(timezone.utc),
                    }
                    await self.timescale_client.store_tariff_element(element_record)

            # Store time-of-use periods
            if "tou_periods" in tariff_data:
                for period in tariff_data["tou_periods"]:
                    period_id = period.get("period_id") or str(uuid.uuid4())
                    period_record = {
                        "period_id": period_id,
                        "tariff_id": tariff_id,
                        "period_name": period["name"],
                        "start_time": period["start_time"],
                        "end_time": period["end_time"],
                        "day_of_week": period.get("day_of_week"),
                        "month": period.get("month"),
                        "day_of_month": period.get("day_of_month"),
                        "price_multiplier": Decimal(str(period.get("price_multiplier", 1.0))),
                        "created_at": datetime.now(timezone.utc),
                    }
                    await self.timescale_client.store_tou_period(period_record)

            self.logger.info(f"Tariff {tariff_id} set for {station_id}")
            return {"status": GenericStatusEnumType.accepted}

        except Exception as e:
            self.logger.error(f"Error setting tariff: {e}")
            return {
                "status": GenericStatusEnumType.rejected,
                "statusInfo": StatusInfoType(reason_code="InternalError", additional_info=str(e)),
            }

    async def calculate_transaction_cost(
        self,
        station_id: str,
        transaction_id: str,
        energy_kwh: float,
        duration_seconds: int,
        max_power_kw: float = 0.0,
    ) -> Dict[str, Any]:
        """Calculate cost for a transaction."""
        self.logger.info(f"Calculating cost for transaction {transaction_id}")

        try:
            # Get active tariffs
            tariffs = await self.timescale_client.get_active_tariffs(station_id)

            if not tariffs:
                # Use default pricing
                return await self._calculate_default_cost(energy_kwh, duration_seconds)

            # Use highest priority tariff
            primary_tariff = tariffs[0]
            tariff_id = primary_tariff["tariff_id"]

            # Get tariff elements
            elements = await self.timescale_client.get_tariff_elements(tariff_id)

            # Calculate costs for each element type
            cost_breakdown = {}
            total_cost = Decimal("0.0")

            for element in elements:
                element_type = element["element_type"]
                price_per_unit = element["price_per_unit"]
                unit = element["unit"]

                # Apply time-of-use multiplier
                tou_multiplier = await self.timescale_client.calculate_tou_multiplier(
                    tariff_id, datetime.now(timezone.utc)
                )
                adjusted_price = price_per_unit * Decimal(str(tou_multiplier))

                if element_type == "Energy" and unit == "kWh":
                    cost = Decimal(str(energy_kwh)) * adjusted_price
                    cost_breakdown["energy_cost"] = float(cost)
                    total_cost += cost

                elif element_type == "Time" and unit in ["hour", "minute"]:
                    if unit == "hour":
                        duration_hours = Decimal(str(duration_seconds)) / Decimal("3600")
                        cost = duration_hours * adjusted_price
                    else:  # minute
                        duration_minutes = Decimal(str(duration_seconds)) / Decimal("60")
                        cost = duration_minutes * adjusted_price
                    cost_breakdown["time_cost"] = float(cost)
                    total_cost += cost

                elif element_type == "Power" and unit == "kW":
                    # Power cost is typically calculated based on maximum power used
                    cost = Decimal(str(max_power_kw)) * adjusted_price
                    cost_breakdown["power_cost"] = float(cost)
                    total_cost += cost

            # Store cost update
            cost_data = {
                "update_id": str(uuid.uuid4()),
                "station_id": station_id,
                "transaction_id": transaction_id,
                "total_cost": float(total_cost),
                "currency": primary_tariff["tariff_currency"],
                "cost_breakdown": cost_breakdown,
                "calculated_at": datetime.now(timezone.utc),
                "created_at": datetime.now(timezone.utc),
            }

            await self.timescale_client.store_cost_update(cost_data)

            self.logger.info(
                f"Calculated cost for transaction {transaction_id}: {total_cost} {primary_tariff['tariff_currency']}"
            )

            return cost_data

        except Exception as e:
            self.logger.error(f"Error calculating transaction cost: {e}")
            return await self._calculate_default_cost(energy_kwh, duration_seconds)

    async def create_cost_updated_notification(
        self,
        station_id: str,
        transaction_id: str,
        evse_id: int,
        connector_id: int,
        cost_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Create CostUpdated notification."""
        try:
            # Create cost breakdown
            costs = []

            for cost_type, amount in cost_data.get("cost_breakdown", {}).items():
                cost_kind = CostKindEnumType.carbon_dioxide_emission  # Default  # noqa: F821

                if cost_type == "energy_cost":
                    cost_kind = CostKindEnumType.energy_cost  # noqa: F821
                elif cost_type == "time_cost":
                    cost_kind = CostKindEnumType.time_cost  # noqa: F821
                elif cost_type == "power_cost":
                    cost_kind = CostKindEnumType.power_cost  # noqa: F821

                cost = CostType(cost_kind=cost_kind, amount=amount)
                costs.append(cost)

            # Create consumed cost
            consumed_cost = ConsumedCostType(  # noqa: F821
                cost=costs, total_cost=cost_data["total_cost"]
            )

            # Create cost updated notification
            cost_updated = CostUpdatedType(  # noqa: F821
                evse_id=evse_id,
                connector_id=connector_id,
                transaction_id=transaction_id,
                consumed_cost=consumed_cost,
            )

            return cost_updated

        except Exception as e:
            self.logger.error(f"Error creating cost updated notification: {e}")
            raise

    async def create_sales_tariff(
        self, station_id: str, tariff_data: Dict[str, Any]
    ) -> SalesTariffType:
        """Create sales tariff for OCPP."""
        try:
            # Get active tariffs
            tariffs = await self.timescale_client.get_active_tariffs(station_id)

            if not tariffs:
                return None

            primary_tariff = tariffs[0]
            tariff_id = primary_tariff["tariff_id"]

            # Get tariff elements and TOU periods
            elements = await self.timescale_client.get_tariff_elements(tariff_id)
            periods = await self.timescale_client.get_tou_periods(tariff_id)

            # Create tariff entries
            entries = []

            for period in periods:
                # Find matching elements for this period
                period_elements = [
                    e for e in elements if e["element_type"] in ["Energy", "Time", "Power"]
                ]

                if period_elements:
                    # Create relative time interval
                    start_time = int(period["start_time"].total_seconds())
                    duration = int((period["end_time"] - period["start_time"]).total_seconds())

                    time_interval = RelativeTimeIntervalType(  # noqa: F821
                        start=start_time, duration=duration
                    )

                    # Create tariff entry
                    entry = SalesTariffEntryType(
                        relative_time_interval=time_interval, e_price_level=1  # Default price level
                    )
                    entries.append(entry)

            # Create sales tariff
            sales_tariff = SalesTariffType(
                id=1,  # Default ID
                sales_tariff_entry=entries,
                sales_tariff_description=primary_tariff.get("tariff_description", "Default Tariff"),
                num_e_price_levels=1,
            )

            return sales_tariff

        except Exception as e:
            self.logger.error(f"Error creating sales tariff: {e}")
            return None

    async def get_cost_history(
        self,
        station_id: str,
        transaction_id: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """Get cost history for station."""
        try:
            cost_updates = await self.timescale_client.get_cost_updates(
                station_id, transaction_id, start_time, end_time
            )
            return cost_updates
        except Exception as e:
            self.logger.error(f"Error getting cost history: {e}")
            return []

    def _validate_tariff_data(self, tariff_data: Dict[str, Any]) -> bool:
        """Validate tariff data."""
        if not tariff_data:
            return False

        # Check required fields
        if "elements" not in tariff_data:
            return False

        # Validate elements
        for element in tariff_data["elements"]:
            if not all(key in element for key in ["type", "price_per_unit", "unit"]):
                return False

            if element["type"] not in [t.value for t in TariffElementType]:
                return False

        # Validate TOU periods if present
        if "tou_periods" in tariff_data:
            for period in tariff_data["tou_periods"]:
                if not all(key in period for key in ["name", "start_time", "end_time"]):
                    return False

        return True

    async def _calculate_default_cost(
        self, energy_kwh: float, duration_seconds: int
    ) -> Dict[str, Any]:
        """Calculate default cost when no tariff is available."""
        # Default pricing: $0.20/kWh
        default_price_per_kwh = Decimal("0.20")
        energy_cost = Decimal(str(energy_kwh)) * default_price_per_kwh

        cost_breakdown = {"energy_cost": float(energy_cost)}

        return {
            "update_id": str(uuid.uuid4()),
            "station_id": "default",
            "transaction_id": "default",
            "total_cost": float(energy_cost),
            "currency": "USD",
            "cost_breakdown": cost_breakdown,
            "calculated_at": datetime.now(timezone.utc),
            "created_at": datetime.now(timezone.utc),
        }

    async def create_default_tariff(self, station_id: str) -> str:
        """Create a default tariff for station."""
        tariff_id = str(uuid.uuid4())

        default_tariff = {
            "tariff_id": tariff_id,
            "description": "Default Energy Tariff",
            "currency": "USD",
            "priority": 0,
            "elements": [
                {"type": "Energy", "price_per_unit": 0.20, "unit": "kWh", "currency": "USD"}
            ],
        }

        await self.set_tariff(station_id, default_tariff)
        self.logger.info(f"Created default tariff {tariff_id} for {station_id}")

        return tariff_id

    async def create_time_of_use_tariff(
        self, station_id: str, peak_price: float = 0.30, off_peak_price: float = 0.15
    ) -> str:
        """Create a time-of-use tariff."""
        tariff_id = str(uuid.uuid4())

        tou_tariff = {
            "tariff_id": tariff_id,
            "description": "Time-of-Use Tariff",
            "currency": "USD",
            "priority": 1,
            "elements": [
                {"type": "Energy", "price_per_unit": peak_price, "unit": "kWh", "currency": "USD"}
            ],
            "tou_periods": [
                {
                    "name": "Peak",
                    "start_time": "08:00:00",
                    "end_time": "18:00:00",
                    "day_of_week": 1,  # Monday
                    "price_multiplier": 1.0,
                },
                {
                    "name": "Peak",
                    "start_time": "08:00:00",
                    "end_time": "18:00:00",
                    "day_of_week": 2,  # Tuesday
                    "price_multiplier": 1.0,
                },
                {
                    "name": "Peak",
                    "start_time": "08:00:00",
                    "end_time": "18:00:00",
                    "day_of_week": 3,  # Wednesday
                    "price_multiplier": 1.0,
                },
                {
                    "name": "Peak",
                    "start_time": "08:00:00",
                    "end_time": "18:00:00",
                    "day_of_week": 4,  # Thursday
                    "price_multiplier": 1.0,
                },
                {
                    "name": "Peak",
                    "start_time": "08:00:00",
                    "end_time": "18:00:00",
                    "day_of_week": 5,  # Friday
                    "price_multiplier": 1.0,
                },
                {
                    "name": "Off-Peak",
                    "start_time": "18:00:00",
                    "end_time": "08:00:00",
                    "day_of_week": 1,  # Monday
                    "price_multiplier": off_peak_price / peak_price,
                },
                {
                    "name": "Off-Peak",
                    "start_time": "18:00:00",
                    "end_time": "08:00:00",
                    "day_of_week": 2,  # Tuesday
                    "price_multiplier": off_peak_price / peak_price,
                },
                {
                    "name": "Off-Peak",
                    "start_time": "18:00:00",
                    "end_time": "08:00:00",
                    "day_of_week": 3,  # Wednesday
                    "price_multiplier": off_peak_price / peak_price,
                },
                {
                    "name": "Off-Peak",
                    "start_time": "18:00:00",
                    "end_time": "08:00:00",
                    "day_of_week": 4,  # Thursday
                    "price_multiplier": off_peak_price / peak_price,
                },
                {
                    "name": "Off-Peak",
                    "start_time": "18:00:00",
                    "end_time": "08:00:00",
                    "day_of_week": 5,  # Friday
                    "price_multiplier": off_peak_price / peak_price,
                },
                {
                    "name": "Weekend",
                    "start_time": "00:00:00",
                    "end_time": "23:59:59",
                    "day_of_week": 6,  # Saturday
                    "price_multiplier": off_peak_price / peak_price,
                },
                {
                    "name": "Weekend",
                    "start_time": "00:00:00",
                    "end_time": "23:59:59",
                    "day_of_week": 0,  # Sunday
                    "price_multiplier": off_peak_price / peak_price,
                },
            ],
        }

        await self.set_tariff(station_id, tou_tariff)
        self.logger.info(f"Created TOU tariff {tariff_id} for {station_id}")

        return tariff_id
