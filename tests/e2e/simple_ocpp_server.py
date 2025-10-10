"""Simple OCPP test server for CitrineOS simulation testing."""

import asyncio
import json
import logging
import websockets
from websockets.server import WebSocketServerProtocol

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class SimpleOCPPServer:
    """Simple OCPP 2.0.1 server for testing."""

    def __init__(self):
        """Initialize server."""
        self.connected_stations = {}
        self.message_handlers = {
            "BootNotification": self.handle_boot_notification,
            "Heartbeat": self.handle_heartbeat,
            "StatusNotification": self.handle_status_notification,
            "MeterValues": self.handle_meter_values,
            "TransactionEvent": self.handle_transaction_event,
            "RequestStartTransaction": self.handle_request_start_transaction,
            "RequestStopTransaction": self.handle_request_stop_transaction,
            "GetVariables": self.handle_get_variables,
            "SetVariables": self.handle_set_variables,
            "SetChargingProfile": self.handle_set_charging_profile,
            "GetChargingProfiles": self.handle_get_charging_profiles,
            "GetCompositeSchedule": self.handle_get_composite_schedule,
            "ClearChargingProfile": self.handle_clear_charging_profile,
            "Reset": self.handle_reset,
            "ChangeAvailability": self.handle_change_availability,
            "TriggerMessage": self.handle_trigger_message,
            "UnlockConnector": self.handle_unlock_connector,
            "GetMonitoringReport": self.handle_get_monitoring_report,
            "SetDisplayMessage": self.handle_set_display_message,
            "ClearDisplayMessage": self.handle_clear_display_message,
            "CustomerInformation": self.handle_customer_information,
            "DeleteCustomerInformation": self.handle_delete_customer_information,
        }

    async def handle_connection(self, websocket: WebSocketServerProtocol, path: str):
        """Handle WebSocket connection."""
        station_id = None
        logger.info(f"New connection from {websocket.remote_address}")
        
        try:
            async for message in websocket:
                try:
                    data = json.loads(message)
                    logger.info(f"Received message: {data}")
                    
                    # Handle OCPP message
                    if len(data) >= 4 and data[0] == 2:  # OCPP 2.0.1 CALL
                        message_id = data[1]
                        action = data[2]
                        payload = data[3]
                        
                        # Extract station ID from boot notification
                        if action == "BootNotification":
                            station_id = payload.get("chargingStation", {}).get("serialNumber", "UNKNOWN")
                            self.connected_stations[station_id] = websocket
                            logger.info(f"Station {station_id} connected")
                        
                        # Handle message
                        if action in self.message_handlers:
                            response = await self.message_handlers[action](payload)
                            await self.send_response(websocket, message_id, action, response)
                        else:
                            logger.warning(f"Unknown action: {action}")
                            await self.send_error(websocket, message_id, action, "NotImplemented")
                    
                except json.JSONDecodeError as e:
                    logger.error(f"Invalid JSON: {e}")
                except Exception as e:
                    logger.error(f"Error processing message: {e}")
                    
        except websockets.exceptions.ConnectionClosed:
            logger.info(f"Connection closed for station {station_id}")
            if station_id and station_id in self.connected_stations:
                del self.connected_stations[station_id]
        except Exception as e:
            logger.error(f"Connection error: {e}")

    async def send_response(self, websocket: WebSocketServerProtocol, message_id: str, action: str, response: dict):
        """Send OCPP response."""
        message = [3, message_id, response]  # OCPP 2.0.1 CALLRESULT
        await websocket.send(json.dumps(message))
        logger.info(f"Sent response for {action}: {response}")

    async def send_error(self, websocket: WebSocketServerProtocol, message_id: str, action: str, error_code: str):
        """Send OCPP error."""
        message = [4, message_id, error_code, "Error", {}]  # OCPP 2.0.1 CALLERROR
        await websocket.send(json.dumps(message))
        logger.info(f"Sent error for {action}: {error_code}")

    # Message handlers
    async def handle_boot_notification(self, payload: dict) -> dict:
        """Handle BootNotification."""
        return {
            "status": "Accepted",
            "currentTime": "2023-01-01T00:00:00Z",
            "interval": 300
        }

    async def handle_heartbeat(self, payload: dict) -> dict:
        """Handle Heartbeat."""
        return {
            "currentTime": "2023-01-01T00:00:00Z"
        }

    async def handle_status_notification(self, payload: dict) -> dict:
        """Handle StatusNotification."""
        return {}  # No response needed

    async def handle_meter_values(self, payload: dict) -> dict:
        """Handle MeterValues."""
        return {}  # No response needed

    async def handle_transaction_event(self, payload: dict) -> dict:
        """Handle TransactionEvent."""
        return {
            "status": "Accepted"
        }

    async def handle_request_start_transaction(self, payload: dict) -> dict:
        """Handle RequestStartTransaction."""
        return {
            "status": "Accepted",
            "transactionId": "TXN123456"
        }

    async def handle_request_stop_transaction(self, payload: dict) -> dict:
        """Handle RequestStopTransaction."""
        return {
            "status": "Accepted"
        }

    async def handle_get_variables(self, payload: dict) -> dict:
        """Handle GetVariables."""
        get_variable_data = payload.get("getVariableData", [])
        get_variable_result = []
        
        for var_data in get_variable_data:
            component_name = var_data.get("component", {}).get("name", "")
            variable_name = var_data.get("variable", {}).get("name", "")
            
            # Return mock values
            if component_name == "ChargingStation" and variable_name == "VendorName":
                attribute_value = "TestVendor"
            elif component_name == "ChargingStation" and variable_name == "Model":
                attribute_value = "TestModel"
            else:
                attribute_value = "Unknown"
            
            get_variable_result.append({
                "component": {"name": component_name},
                "variable": {"name": variable_name},
                "attributeStatus": "Accepted",
                "attributeValue": attribute_value
            })
        
        return {
            "status": "Accepted",
            "getVariableResult": get_variable_result
        }

    async def handle_set_variables(self, payload: dict) -> dict:
        """Handle SetVariables."""
        set_variable_data = payload.get("setVariableData", [])
        set_variable_result = []
        
        for var_data in set_variable_data:
            component_name = var_data.get("component", {}).get("name", "")
            variable_name = var_data.get("variable", {}).get("name", "")
            
            set_variable_result.append({
                "component": {"name": component_name},
                "variable": {"name": variable_name},
                "attributeStatus": "Accepted"
            })
        
        return {
            "status": "Accepted",
            "setVariableResult": set_variable_result
        }

    async def handle_set_charging_profile(self, payload: dict) -> dict:
        """Handle SetChargingProfile."""
        return {
            "status": "Accepted"
        }

    async def handle_get_charging_profiles(self, payload: dict) -> dict:
        """Handle GetChargingProfiles."""
        return {
            "status": "Accepted",
            "chargingProfile": []
        }

    async def handle_get_composite_schedule(self, payload: dict) -> dict:
        """Handle GetCompositeSchedule."""
        return {
            "status": "Accepted",
            "chargingSchedule": {
                "id": 1,
                "chargingRateUnit": "W",
                "chargingSchedulePeriod": [
                    {
                        "startPeriod": 0,
                        "limit": 22.0
                    }
                ],
                "duration": 3600
            }
        }

    async def handle_clear_charging_profile(self, payload: dict) -> dict:
        """Handle ClearChargingProfile."""
        return {
            "status": "Accepted"
        }

    async def handle_reset(self, payload: dict) -> dict:
        """Handle Reset."""
        return {
            "status": "Accepted"
        }

    async def handle_change_availability(self, payload: dict) -> dict:
        """Handle ChangeAvailability."""
        return {
            "status": "Accepted"
        }

    async def handle_trigger_message(self, payload: dict) -> dict:
        """Handle TriggerMessage."""
        return {
            "status": "Accepted"
        }

    async def handle_unlock_connector(self, payload: dict) -> dict:
        """Handle UnlockConnector."""
        return {
            "status": "Accepted"
        }

    async def handle_get_monitoring_report(self, payload: dict) -> dict:
        """Handle GetMonitoringReport."""
        return {
            "status": "Accepted",
            "monitoringReport": {
                "id": "REPORT_001",
                "monitoringData": []
            }
        }

    async def handle_set_display_message(self, payload: dict) -> dict:
        """Handle SetDisplayMessage."""
        return {
            "status": "Accepted"
        }

    async def handle_clear_display_message(self, payload: dict) -> dict:
        """Handle ClearDisplayMessage."""
        return {
            "status": "Accepted"
        }

    async def handle_customer_information(self, payload: dict) -> dict:
        """Handle CustomerInformation."""
        return {
            "status": "Accepted",
            "customerInformation": {
                "customerIdentifier": payload.get("customerIdentifier", "ANONYMIZED")
            }
        }

    async def handle_delete_customer_information(self, payload: dict) -> dict:
        """Handle DeleteCustomerInformation."""
        return {
            "status": "Accepted"
        }

    async def start_server(self, host: str = "localhost", port: int = 9000):
        """Start the WebSocket server."""
        logger.info(f"Starting OCPP server on {host}:{port}")
        
        async with websockets.serve(self.handle_connection, host, port):
            logger.info(f"OCPP server running on ws://{host}:{port}")
            await asyncio.Future()  # Run forever


async def main():
    """Main function."""
    server = SimpleOCPPServer()
    await server.start_server()


if __name__ == "__main__":
    asyncio.run(main())
