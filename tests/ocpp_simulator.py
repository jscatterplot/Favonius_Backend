"""OCPP 2.0.1 Message Simulator for Testing."""

import asyncio
import json
import uuid
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional
import websockets
from websockets.server import WebSocketServerProtocol

from websocket_handler.config import Config
from websocket_handler.timescale_client import TimescaleClient
from websocket_handler.ocpp_handler import EnhancedOCPPChargePoint


class OCPPSimulator:
    """Simulates an OCPP 2.0.1 charging station for testing."""
    
    def __init__(self, station_id: str = "TEST_STATION_001"):
        self.station_id = station_id
        self.websocket = None
        self.charge_point = None
        self.message_queue = asyncio.Queue()
        self.connected = False
        
    async def connect(self, server_url: str = "ws://localhost:8080/ocpp/TEST_STATION_001"):
        """Connect to OCPP server."""
        try:
            self.websocket = await websockets.connect(server_url)
            self.connected = True
            print(f"✅ Connected to {server_url}")
            
            # Start message handler
            asyncio.create_task(self._handle_messages())
            
            return True
        except Exception as e:
            print(f"❌ Failed to connect: {e}")
            return False
    
    async def disconnect(self):
        """Disconnect from server."""
        if self.websocket:
            await self.websocket.close()
            self.connected = False
            print("🔌 Disconnected from server")
    
    async def _handle_messages(self):
        """Handle incoming messages."""
        try:
            async for message in self.websocket:
                try:
                    data = json.loads(message)
                    await self.message_queue.put(data)
                    print(f"📨 Received: {data.get('action', 'Unknown')}")
                except json.JSONDecodeError:
                    print(f"❌ Invalid JSON: {message}")
        except websockets.exceptions.ConnectionClosed:
            print("🔌 Connection closed")
            self.connected = False
    
    async def send_message(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Send OCPP message and wait for response."""
        if not self.connected:
            raise Exception("Not connected to server")
        
        message_id = str(uuid.uuid4())
        message["messageId"] = message_id
        
        await self.websocket.send(json.dumps(message))
        print(f"📤 Sent: {message['action']}")
        
        # Wait for response
        timeout = 10.0
        start_time = asyncio.get_event_loop().time()
        
        while True:
            try:
                response = await asyncio.wait_for(self.message_queue.get(), timeout=1.0)
                if response.get("messageId") == message_id:
                    return response
            except asyncio.TimeoutError:
                if asyncio.get_event_loop().time() - start_time > timeout:
                    raise Exception(f"Timeout waiting for response to {message['action']}")
                continue
    
    async def boot_notification(self) -> Dict[str, Any]:
        """Send BootNotification."""
        message = {
            "action": "BootNotification",
            "payload": {
                "chargingStation": {
                    "model": "TestModel",
                    "vendorName": "TestVendor",
                    "serialNumber": "TEST123",
                    "firmwareVersion": "1.0.0"
                },
                "reason": "PowerUp"
            }
        }
        return await self.send_message(message)
    
    async def status_notification(self, connector_id: int = 1, status: str = "Available") -> Dict[str, Any]:
        """Send StatusNotification."""
        message = {
            "action": "StatusNotification",
            "payload": {
                "connectorId": connector_id,
                "errorCode": "NoError",
                "status": status,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        }
        return await self.send_message(message)
    
    async def heartbeat(self) -> Dict[str, Any]:
        """Send Heartbeat."""
        message = {
            "action": "Heartbeat",
            "payload": {}
        }
        return await self.send_message(message)
    
    async def get_variables(self, variables: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Send GetVariables."""
        message = {
            "action": "GetVariables",
            "payload": {
                "getVariableData": variables
            }
        }
        return await self.send_message(message)
    
    async def set_variables(self, variables: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Send SetVariables."""
        message = {
            "action": "SetVariables",
            "payload": {
                "setVariableData": variables
            }
        }
        return await self.send_message(message)
    
    async def get_base_report(self, report_base: str = "ConfigurationInventory") -> Dict[str, Any]:
        """Send GetBaseReport."""
        message = {
            "action": "GetBaseReport",
            "payload": {
                "requestId": 1,
                "reportBase": report_base
            }
        }
        return await self.send_message(message)
    
    async def set_charging_profile(self, profile: Dict[str, Any]) -> Dict[str, Any]:
        """Send SetChargingProfile."""
        message = {
            "action": "SetChargingProfile",
            "payload": profile
        }
        return await self.send_message(message)
    
    async def clear_charging_profile(self, profile_id: Optional[int] = None) -> Dict[str, Any]:
        """Send ClearChargingProfile."""
        payload = {}
        if profile_id is not None:
            payload["chargingProfileId"] = profile_id
        
        message = {
            "action": "ClearChargingProfile",
            "payload": payload
        }
        return await self.send_message(message)
    
    async def get_composite_schedule(self, evse_id: int = 1, duration: int = 3600) -> Dict[str, Any]:
        """Send GetCompositeSchedule."""
        message = {
            "action": "GetCompositeSchedule",
            "payload": {
                "requestId": 1,
                "evseId": evse_id,
                "duration": duration,
                "chargingRateUnit": "W"
            }
        }
        return await self.send_message(message)
    
    async def request_start_transaction(self, evse_id: int = 1, id_token: str = "test_token") -> Dict[str, Any]:
        """Send RequestStartTransaction."""
        message = {
            "action": "RequestStartTransaction",
            "payload": {
                "evseId": evse_id,
                "idToken": {
                    "idToken": id_token,
                    "type": "ISO14443"
                }
            }
        }
        return await self.send_message(message)
    
    async def request_stop_transaction(self, transaction_id: str) -> Dict[str, Any]:
        """Send RequestStopTransaction."""
        message = {
            "action": "RequestStopTransaction",
            "payload": {
                "transactionId": transaction_id,
                "reason": "Remote"
            }
        }
        return await self.send_message(message)
    
    async def reset(self, reset_type: str = "Immediate") -> Dict[str, Any]:
        """Send Reset."""
        message = {
            "action": "Reset",
            "payload": {
                "type": reset_type
            }
        }
        return await self.send_message(message)
    
    async def change_availability(self, operational_status: str = "Operative") -> Dict[str, Any]:
        """Send ChangeAvailability."""
        message = {
            "action": "ChangeAvailability",
            "payload": {
                "operationalStatus": operational_status,
                "evse": {
                    "id": 1
                }
            }
        }
        return await self.send_message(message)
    
    async def trigger_message(self, requested_message: str = "StatusNotification") -> Dict[str, Any]:
        """Send TriggerMessage."""
        message = {
            "action": "TriggerMessage",
            "payload": {
                "requestedMessage": requested_message,
                "evse": {
                    "id": 1
                }
            }
        }
        return await self.send_message(message)
    
    async def unlock_connector(self, evse_id: int = 1, connector_id: int = 1) -> Dict[str, Any]:
        """Send UnlockConnector."""
        message = {
            "action": "UnlockConnector",
            "payload": {
                "evseId": evse_id,
                "connectorId": connector_id
            }
        }
        return await self.send_message(message)
    
    async def get_15118_ev_certificate(self, certificate_type: str = "V2GRootCertificate") -> Dict[str, Any]:
        """Send Get15118EVCertificate."""
        message = {
            "action": "Get15118EVCertificate",
            "payload": {
                "certificateType": certificate_type
            }
        }
        return await self.send_message(message)
    
    async def install_certificate(self, certificate_type: str = "V2GRootCertificate", certificate: str = "test_cert") -> Dict[str, Any]:
        """Send InstallCertificate."""
        message = {
            "action": "InstallCertificate",
            "payload": {
                "certificateType": certificate_type,
                "certificate": certificate
            }
        }
        return await self.send_message(message)


async def run_simulation():
    """Run a complete OCPP simulation."""
    print("🚀 Starting OCPP 2.0.1 Simulation")
    print("=" * 50)
    
    simulator = OCPPSimulator()
    
    try:
        # Connect to server
        if not await simulator.connect():
            print("❌ Failed to connect to server")
            return
        
        # Test basic messages
        print("\n📡 Testing basic OCPP messages...")
        
        # Boot notification
        response = await simulator.boot_notification()
        print(f"BootNotification: {response.get('status', 'Unknown')}")
        
        # Status notification
        response = await simulator.status_notification()
        print(f"StatusNotification: {response.get('status', 'Unknown')}")
        
        # Heartbeat
        response = await simulator.heartbeat()
        print(f"Heartbeat: {response.get('status', 'Unknown')}")
        
        # Test device management
        print("\n🔧 Testing device management...")
        
        # Get variables
        variables = [{
            "component": {"name": "ChargingStation", "instance": ""},
            "variable": {"name": "Model", "instance": ""},
            "attributeType": "Actual"
        }]
        response = await simulator.get_variables(variables)
        print(f"GetVariables: {response.get('status', 'Unknown')}")
        
        # Set variables
        variables = [{
            "component": {"name": "ChargingStation", "instance": ""},
            "variable": {"name": "Model", "instance": ""},
            "attributeType": "Actual",
            "attributeValue": "TestModel"
        }]
        response = await simulator.set_variables(variables)
        print(f"SetVariables: {response.get('status', 'Unknown')}")
        
        # Get base report
        response = await simulator.get_base_report()
        print(f"GetBaseReport: {response.get('status', 'Unknown')}")
        
        # Test charging profiles
        print("\n⚡ Testing charging profiles...")
        
        # Set charging profile
        profile = {
            "id": 1,
            "stackLevel": 1,
            "chargingProfilePurpose": "TxProfile",
            "chargingProfileKind": "Absolute",
            "chargingSchedule": {
                "id": 1,
                "startSchedule": datetime.now(timezone.utc).isoformat(),
                "duration": 3600,
                "chargingRateUnit": "W",
                "chargingSchedulePeriod": [
                    {
                        "startPeriod": 0,
                        "limit": 22.0,
                        "numberPhases": 3
                    }
                ]
            }
        }
        response = await simulator.set_charging_profile(profile)
        print(f"SetChargingProfile: {response.get('status', 'Unknown')}")
        
        # Get composite schedule
        response = await simulator.get_composite_schedule()
        print(f"GetCompositeSchedule: {response.get('status', 'Unknown')}")
        
        # Clear charging profile
        response = await simulator.clear_charging_profile(1)
        print(f"ClearChargingProfile: {response.get('status', 'Unknown')}")
        
        # Test transactions
        print("\n💳 Testing transactions...")
        
        # Request start transaction
        response = await simulator.request_start_transaction()
        print(f"RequestStartTransaction: {response.get('status', 'Unknown')}")
        
        transaction_id = response.get("transactionId")
        if transaction_id:
            # Request stop transaction
            response = await simulator.request_stop_transaction(transaction_id)
            print(f"RequestStopTransaction: {response.get('status', 'Unknown')}")
        
        # Test device control
        print("\n🎛️ Testing device control...")
        
        # Reset
        response = await simulator.reset()
        print(f"Reset: {response.get('status', 'Unknown')}")
        
        # Change availability
        response = await simulator.change_availability()
        print(f"ChangeAvailability: {response.get('status', 'Unknown')}")
        
        # Trigger message
        response = await simulator.trigger_message()
        print(f"TriggerMessage: {response.get('status', 'Unknown')}")
        
        # Unlock connector
        response = await simulator.unlock_connector()
        print(f"UnlockConnector: {response.get('status', 'Unknown')}")
        
        # Test certificates
        print("\n🔐 Testing certificates...")
        
        # Get 15118 EV certificate
        response = await simulator.get_15118_ev_certificate()
        print(f"Get15118EVCertificate: {response.get('status', 'Unknown')}")
        
        # Install certificate
        response = await simulator.install_certificate()
        print(f"InstallCertificate: {response.get('status', 'Unknown')}")
        
        print("\n🎉 Simulation completed successfully!")
        
    except Exception as e:
        print(f"\n❌ Simulation failed: {e}")
    
    finally:
        await simulator.disconnect()


if __name__ == "__main__":
    asyncio.run(run_simulation())
