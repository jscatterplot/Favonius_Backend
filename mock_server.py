#!/usr/bin/env python3
"""Mock WebSocket server for load and e2e tests."""

import asyncio
import json
import logging
import websockets
from websockets.server import WebSocketServerProtocol

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class MockOCPPServer:
    """Mock OCPP server for testing."""
    
    def __init__(self):
        self.connections = {}
        
    async def handle_connection(self, websocket: WebSocketServerProtocol):
        """Handle WebSocket connection."""
        station_id = "UNKNOWN"
        self.connections[station_id] = websocket
        
        logger.info(f"Station {station_id} connected from {websocket.remote_address}")
        
        try:
            async for message in websocket:
                try:
                    # Parse OCPP message
                    data = json.loads(message)
                    message_type = data[0]
                    message_id = data[1]
                    action = data[2]
                    payload = data[3] if len(data) > 3 else {}
                    
                    logger.info(f"Received {action} from {station_id}")
                    
                    # Handle different OCPP actions
                    if action == "BootNotification":
                        response = [3, message_id, {
                            "status": "Accepted",
                            "currentTime": "2024-01-01T00:00:00.000Z",
                            "interval": 300
                        }]
                    elif action == "Heartbeat":
                        response = [3, message_id, {
                            "currentTime": "2024-01-01T00:00:00.000Z"
                        }]
                    elif action == "StatusNotification":
                        response = [3, message_id, {}]
                    elif action == "MeterValues":
                        response = [3, message_id, {}]
                    elif action == "TransactionEvent":
                        response = [3, message_id, {}]
                    else:
                        # Generic response for other actions
                        response = [3, message_id, {"status": "Accepted"}]
                    
                    # Send response
                    await websocket.send(json.dumps(response))
                    logger.info(f"Sent response for {action} to {station_id}")
                    
                except json.JSONDecodeError:
                    logger.error(f"Invalid JSON from {station_id}: {message}")
                except Exception as e:
                    logger.error(f"Error handling message from {station_id}: {e}")
                    
        except websockets.exceptions.ConnectionClosed:
            logger.info(f"Station {station_id} disconnected")
        except Exception as e:
            logger.error(f"Connection error for {station_id}: {e}")
        finally:
            if station_id in self.connections:
                del self.connections[station_id]

async def main():
    """Run the mock server."""
    server = MockOCPPServer()
    
    logger.info("Starting mock OCPP server on port 9000...")
    
    async with websockets.serve(
        server.handle_connection,
        "0.0.0.0",
        9000
    ):
        logger.info("Mock server started. Press Ctrl+C to stop.")
        try:
            await asyncio.Future()  # Run forever
        except KeyboardInterrupt:
            logger.info("Shutting down...")

if __name__ == "__main__":
    asyncio.run(main())
