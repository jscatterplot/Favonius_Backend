#!/usr/bin/env python3
"""Example demonstrating enhanced OCPP 2.1 functionality with V2G capabilities."""

import asyncio
import json
import logging
from datetime import datetime, timezone

try:
    import websockets
except ModuleNotFoundError:
    print("This example relies on the 'websockets' package.")
    print("Please install it by running: ")
    print()
    print(" $ pip install websockets")
    import sys
    sys.exit(1)

from ocpp.v21 import call
from ocpp.v21.datatypes import ChargingStationType, ChargingProfileType, ChargingScheduleType, ChargingSchedulePeriodType
from ocpp.v21.enums import ChargingProfilePurposeEnumType, ChargingRateUnitEnumType, ChargingProfileKindEnumType

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class EnhancedChargePoint:
    """Enhanced charge point demonstrating V2G capabilities."""
    
    def __init__(self, station_id: str, websocket):
        self.station_id = station_id
        self.websocket = websocket
        self.connected = False
        
    async def start(self):
        """Start the charge point and handle messages."""
        try:
            # Send boot notification
            await self.send_boot_notification()
            
            # Start heartbeat
            asyncio.create_task(self.send_heartbeat())
            
            # Handle incoming messages
            async for message in self.websocket:
                await self.handle_message(message)
                
        except websockets.exceptions.ConnectionClosed:
            logger.info(f"Connection closed for {self.station_id}")
        except Exception as e:
            logger.error(f"Error in charge point {self.station_id}: {e}")
    
    async def send_boot_notification(self):
        """Send boot notification to central system."""
        request = call.BootNotification(
            charging_station=ChargingStationType(
                serial_number=self.station_id,
                model="Enhanced V2G Charger",
                vendor_name="Favonius Energy"
            ),
            reason="PowerUp"
        )
        
        response = await self.call(request)
        
        if response.status == "Accepted":
            logger.info(f"Boot notification accepted for {self.station_id}")
            self.connected = True
        else:
            logger.warning(f"Boot notification rejected for {self.station_id}")
    
    async def send_heartbeat(self):
        """Send periodic heartbeat."""
        while True:
            try:
                request = call.Heartbeat()
                response = await self.call(request)
                logger.debug(f"Heartbeat response: {response.current_time}")
                await asyncio.sleep(30)  # Send every 30 seconds
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")
                break
    
    async def send_status_notification(self, connector_id: int, status: str):
        """Send status notification."""
        request = call.StatusNotification(
            connector_id=connector_id,
            error_code="NoError",
            status=status,
            timestamp=datetime.now(timezone.utc).isoformat()
        )
        
        response = await self.call(request)
        logger.info(f"Status notification sent for connector {connector_id}: {status}")
    
    async def send_meter_values(self, evse_id: int, power_kw: float, soc_percent: float):
        """Send meter values with power and SOC data."""
        request = call.MeterValues(
            evse_id=evse_id,
            meter_value=[{
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "sampledValue": [
                    {
                        "value": str(power_kw * 1000),  # Convert to W
                        "measurand": "Power.Active.Import",
                        "unitOfMeasure": {"unit": "W"}
                    },
                    {
                        "value": str(soc_percent),
                        "measurand": "SoC",
                        "unitOfMeasure": {"unit": "Percent"}
                    }
                ]
            }]
        )
        
        response = await self.call(request)
        logger.info(f"Meter values sent: {power_kw}kW, {soc_percent}% SoC")
    
    async def send_ev_charging_needs(self, evse_id: int, requested_energy: float, departure_time: str):
        """Send EV charging needs for V2G optimization."""
        request = call.NotifyEVChargingNeeds(
            evse_id=evse_id,
            charging_needs={
                "requestedEnergyTransfer": requested_energy,
                "departureTime": departure_time,
                "acChargingParameters": {
                    "energyAmount": requested_energy,
                    "evMinCurrent": 6,
                    "evMaxCurrent": 32,
                    "evMaxVoltage": 230
                }
            }
        )
        
        response = await self.call(request)
        logger.info(f"EV charging needs sent: {requested_energy}kWh by {departure_time}")
    
    async def send_ev_charging_schedule(self, evse_id: int, schedule_periods: list):
        """Send EV's proposed charging schedule."""
        request = call.NotifyEVChargingSchedule(
            evse_id=evse_id,
            time_base=datetime.now(timezone.utc).isoformat(),
            charging_schedule={
                "id": 1,
                "chargingSchedulePeriod": schedule_periods,
                "chargingRateUnit": "W",
                "duration": 3600  # 1 hour
            }
        )
        
        response = await self.call(request)
        logger.info(f"EV charging schedule sent with {len(schedule_periods)} periods")
    
    async def call(self, request):
        """Send OCPP call and wait for response."""
        # This is a simplified version - in practice you'd use the OCPP library
        message = [2, "unique_id", request.__class__.__name__, request.__dict__]
        await self.websocket.send(json.dumps(message))
        
        # Wait for response (simplified)
        response_data = await self.websocket.recv()
        response = json.loads(response_data)
        
        # Return mock response for demo
        return type('Response', (), {
            'status': 'Accepted',
            'current_time': datetime.now(timezone.utc).isoformat()
        })()
    
    async def handle_message(self, message):
        """Handle incoming OCPP messages."""
        try:
            data = json.loads(message)
            action = data[2] if len(data) > 2 else "Unknown"
            logger.info(f"Received {action} message")
            
            # Handle SetChargingProfile
            if action == "SetChargingProfile":
                await self.handle_set_charging_profile(data[3])
            elif action == "SetDERControl":
                await self.handle_set_der_control(data[3])
            elif action == "ClearDERControl":
                await self.handle_clear_der_control()
                
        except Exception as e:
            logger.error(f"Error handling message: {e}")
    
    async def handle_set_charging_profile(self, payload):
        """Handle SetChargingProfile command."""
        charging_profile = payload.get("chargingProfile", {})
        logger.info(f"Received charging profile: {charging_profile.get('id', 'unknown')}")
        
        # In a real implementation, you would apply the charging profile
        # to control the charger's power output
        
    async def handle_set_der_control(self, payload):
        """Handle SetDERControl command for V2G operations."""
        der_control = payload.get("derControl", {})
        logger.info(f"Received DER control: {der_control.get('id', 'unknown')}")
        
        # In a real implementation, you would enable V2G mode
        # and start responding to grid signals
        
    async def handle_clear_der_control(self):
        """Handle ClearDERControl command."""
        logger.info("DER control cleared - returning to normal charging mode")


async def main():
    """Main example function."""
    # Connect to the central system
    uri = "ws://localhost:9000/CP_001"
    
    async with websockets.connect(uri, subprotocols=["ocpp2.1"]) as websocket:
        charge_point = EnhancedChargePoint("CP_001", websocket)
        
        # Start the charge point
        await charge_point.start()


if __name__ == "__main__":
    asyncio.run(main())