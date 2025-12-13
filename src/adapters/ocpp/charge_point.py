"""OCPP Charge Point management for fleet optimization.

Reference: Development plan Step 3.1, PRD_v2.md#9-1-ocpp-integration
Per PRD Section 8.4, max_charge_kw from OCPP MeterValues is extracted
and dynamically updated in the vehicles table.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Optional

from ocpp.v16 import ChargePoint as CP16
from ocpp.v16 import call, call_result
from ocpp.routing import on

logger = logging.getLogger(__name__)


class FleetChargePoint(CP16):
    """Custom ChargePoint handler for fleet optimization.

    Handles OCPP 1.6-J messages from EV chargers and provides methods
    to send charging profiles from the optimization engine.

    Reference: PRD Section 9.1, Development plan Step 3.1
    """

    def __init__(
        self,
        id: str,
        connection,
        on_status_change: Optional[Callable[[str, int, str], None]] = None,
        on_meter_values: Optional[
            Callable[[str, int, float, float, datetime, Optional[float]], None]
        ] = None,
    ):
        """Initialize FleetChargePoint.

        Args:
            id: Charge point identifier
            connection: WebSocket connection
            on_status_change: Optional callback for status changes
                Signature: (charge_point_id, connector_id, status) -> None
            on_meter_values: Optional callback for meter value updates
                Signature: (charge_point_id, connector_id, soc, power_kw, timestamp, max_charge_kw) -> None
                max_charge_kw is optional and may be None if not reported by charger
        """
        super().__init__(id, connection)
        self.on_status_change = on_status_change
        self.on_meter_values = on_meter_values
        self.current_transaction_id: Optional[int] = None
        logger.info(f"Initialized FleetChargePoint: {id}")

    @on('BootNotification')
    async def on_boot_notification(
        self, charge_point_vendor: str, charge_point_model: str, **kwargs
    ):
        """Handle BootNotification from charger.

        Args:
            charge_point_vendor: Vendor name
            charge_point_model: Model name
            **kwargs: Additional boot notification fields

        Returns:
            BootNotificationPayload with Accepted status and 300s interval
        """
        logger.info(
            f"BootNotification from {self.id}: {charge_point_vendor} {charge_point_model}"
        )
        return call_result.BootNotificationPayload(
            current_time=datetime.utcnow().isoformat(),
            interval=300,  # 5 minutes heartbeat per PRD Section 9.1
            status='Accepted',
        )

    @on('StatusNotification')
    async def on_status_notification(
        self, connector_id: int, error_code: str, status: str, **kwargs
    ):
        """Handle StatusNotification from charger.

        Args:
            connector_id: Connector identifier
            error_code: Error code (if any)
            status: Current connector status
            **kwargs: Additional status notification fields

        Returns:
            StatusNotificationPayload
        """
        logger.debug(
            f"StatusNotification from {self.id}, connector {connector_id}: {status}"
        )
        if self.on_status_change:
            try:
                await self.on_status_change(self.id, connector_id, status)
            except Exception as e:
                logger.error(f"Error in status change callback: {e}")
        return call_result.StatusNotificationPayload()

    @on('MeterValues')
    async def on_meter_values(self, connector_id: int, meter_value: list, **kwargs):
        """Handle MeterValues from charger.

        Extracts SoC and power readings, converts units, and calls callback.

        Args:
            connector_id: Connector identifier
            meter_value: List of meter value objects
            **kwargs: Additional meter values fields

        Returns:
            MeterValuesPayload
        """
        soc: Optional[float] = None
        power_kw: Optional[float] = None
        max_charge_kw: Optional[float] = None
        timestamp: Optional[datetime] = None

        for mv in meter_value:
            # Extract timestamp
            if 'timestamp' in mv:
                timestamp_str = mv['timestamp']
                if isinstance(timestamp_str, str):
                    timestamp = datetime.fromisoformat(
                        timestamp_str.replace('Z', '+00:00')
                    )
                elif isinstance(timestamp_str, datetime):
                    timestamp = timestamp_str

            # Extract sampled values
            for sv in mv.get('sampledValue', []):
                measurand = sv.get('measurand', '')
                value_str = sv.get('value', '0')
                unit = sv.get('unit', '')

                try:
                    value = float(value_str)
                except (ValueError, TypeError):
                    logger.warning(
                        f"Invalid meter value from {self.id}: {value_str}"
                    )
                    continue

                if measurand == 'SoC':
                    # Convert from 0-100% to 0.0-1.0
                    soc = value / 100.0
                elif measurand == 'Power.Active.Import':
                    # Convert from W to kW
                    if unit == 'W':
                        power_kw = value / 1000.0
                    elif unit == 'kW':
                        power_kw = value
                    else:
                        power_kw = value / 1000.0  # Default assume W
                elif measurand == 'maxChargingRate' or measurand == 'MaxChargingRate':
                    # Extract max charge rate from OCPP (per PRD Section 8.4)
                    # Convert from W to kW if needed
                    if unit == 'W':
                        max_charge_kw = value / 1000.0
                    elif unit == 'kW':
                        max_charge_kw = value
                    else:
                        max_charge_kw = value / 1000.0  # Default assume W

        if soc is not None or power_kw is not None:
            logger.debug(
                f"MeterValues from {self.id}, connector {connector_id}: "
                f"SoC={soc}, Power={power_kw}kW"
                + (f", max_charge_kw={max_charge_kw}kW" if max_charge_kw else "")
            )
            if self.on_meter_values and timestamp:
                try:
                    # Pass max_charge_kw to callback if available
                    await self.on_meter_values(
                        self.id, connector_id, soc or 0.0, power_kw or 0.0, timestamp, max_charge_kw
                    )
                except Exception as e:
                    logger.error(f"Error in meter values callback: {e}")

        return call_result.MeterValuesPayload()

    @on('StartTransaction')
    async def on_start_transaction(
        self, connector_id: int, id_tag: str, meter_start: int, timestamp: str, **kwargs
    ):
        """Handle StartTransaction from charger.

        Args:
            connector_id: Connector identifier
            id_tag: ID tag of the user
            meter_start: Initial meter value
            timestamp: Transaction start timestamp
            **kwargs: Additional transaction fields

        Returns:
            StartTransactionPayload with transaction ID
        """
        # Generate transaction ID (simple increment, in production use UUID)
        import random
        self.current_transaction_id = random.randint(1000, 9999)
        logger.info(
            f"StartTransaction from {self.id}, connector {connector_id}, "
            f"transaction_id={self.current_transaction_id}"
        )
        return call_result.StartTransactionPayload(
            transaction_id=self.current_transaction_id,
            id_tag_info={'status': 'Accepted'},
        )

    @on('StopTransaction')
    async def on_stop_transaction(
        self,
        transaction_id: int,
        id_tag: str,
        meter_stop: int,
        timestamp: str,
        **kwargs,
    ):
        """Handle StopTransaction from charger.

        Args:
            transaction_id: Transaction identifier
            id_tag: ID tag of the user
            meter_stop: Final meter value
            timestamp: Transaction stop timestamp
            **kwargs: Additional transaction fields

        Returns:
            StopTransactionPayload
        """
        logger.info(
            f"StopTransaction from {self.id}, transaction_id={transaction_id}"
        )
        if self.current_transaction_id == transaction_id:
            self.current_transaction_id = None
        return call_result.StopTransactionPayload(id_tag_info={'status': 'Accepted'})

    async def set_charging_profile(
        self,
        connector_id: int,
        charging_schedule: list[dict],
        max_retries: int = 3,
    ) -> bool:
        """Send SetChargingProfile to charger with retry logic.

        Args:
            connector_id: Connector identifier
            charging_schedule: List of charging schedule periods
                Each period: {'startPeriod': int, 'limit': int, 'numberPhases': int}
            max_retries: Maximum number of retry attempts (default 3)

        Returns:
            True if accepted, False otherwise
        """
        import asyncio

        for attempt in range(max_retries):
            try:
                payload = call.SetChargingProfilePayload(
                    connector_id=connector_id,
                    cs_charging_profiles={
                        'charging_profile_id': 1,
                        'stack_level': 0,
                        'charging_profile_purpose': 'TxProfile',
                        'charging_profile_kind': 'Absolute',
                        'charging_schedule': {
                            'charging_rate_unit': 'W',
                            'charging_schedule_period': charging_schedule,
                        },
                    },
                )

                response = await self.call(payload)
                accepted = response.status == 'Accepted'

                if accepted:
                    logger.info(
                        f"SetChargingProfile to {self.id}, connector {connector_id}: "
                        f"Accepted (attempt {attempt + 1})"
                    )
                    return True
                else:
                    logger.warning(
                        f"SetChargingProfile to {self.id}, connector {connector_id}: "
                        f"Rejected (attempt {attempt + 1}/{max_retries})"
                    )
                    if attempt < max_retries - 1:
                        await asyncio.sleep(1.0)  # Wait before retry

            except Exception as e:
                logger.error(
                    f"Error sending SetChargingProfile to {self.id} "
                    f"(attempt {attempt + 1}/{max_retries}): {e}"
                )
                if attempt < max_retries - 1:
                    await asyncio.sleep(1.0)  # Wait before retry

        logger.error(
            f"SetChargingProfile to {self.id}, connector {connector_id}: "
            f"Failed after {max_retries} attempts"
        )
        return False

    async def remote_start_transaction(self, connector_id: int, id_tag: str) -> bool:
        """Start charging session remotely.

        Args:
            connector_id: Connector identifier
            id_tag: ID tag for authorization

        Returns:
            True if accepted, False otherwise
        """
        try:
            payload = call.RemoteStartTransactionPayload(
                connector_id=connector_id,
                id_tag=id_tag,
            )
            response = await self.call(payload)
            accepted = response.status == 'Accepted'
            logger.info(
                f"RemoteStartTransaction to {self.id}, connector {connector_id}: "
                f"{'Accepted' if accepted else 'Rejected'}"
            )
            return accepted
        except Exception as e:
            logger.error(f"Error sending RemoteStartTransaction to {self.id}: {e}")
            return False

    async def remote_stop_transaction(self, transaction_id: int) -> bool:
        """Stop charging session remotely.

        Args:
            transaction_id: Transaction identifier

        Returns:
            True if accepted, False otherwise
        """
        try:
            payload = call.RemoteStopTransactionPayload(transaction_id=transaction_id)
            response = await self.call(payload)
            accepted = response.status == 'Accepted'
            logger.info(
                f"RemoteStopTransaction to {self.id}, transaction_id={transaction_id}: "
                f"{'Accepted' if accepted else 'Rejected'}"
            )
            return accepted
        except Exception as e:
            logger.error(f"Error sending RemoteStopTransaction to {self.id}: {e}")
            return False


def convert_schedule_to_ocpp_profile(
    schedule: list[tuple[int, float]], delta_t: float = 0.25, number_phases: int = 3
) -> list[dict]:
    """Convert optimization schedule to OCPP charging profile format.

    Args:
        schedule: List of (timestep, power_kw) tuples from optimization
        delta_t: Time step duration in hours (default 0.25 = 15 minutes)
        number_phases: Number of phases for AC charging (default 3)

    Returns:
        List of charging schedule period dictionaries:
        [{'startPeriod': int, 'limit': int, 'numberPhases': int}, ...]

    Example:
        >>> schedule = [(0, 80.0), (1, 60.0), (2, 80.0)]
        >>> profile = convert_schedule_to_ocpp_profile(schedule, delta_t=0.25)
        >>> # Returns periods with startPeriod in seconds, limit in Watts
    """
    periods = []
    for timestep, power_kw in schedule:
        # Convert timestep to seconds: timestep * delta_t (hours) * 3600
        start_period = int(timestep * delta_t * 3600)
        # Convert power from kW to W
        limit_watts = int(power_kw * 1000)
        periods.append({
            'startPeriod': start_period,
            'limit': limit_watts,
            'numberPhases': number_phases,
        })
    return periods

