"""CitrineOS charging station simulation for end-to-end testing."""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import websockets

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class CitrineOSSimulator:
    """Simulates CitrineOS charging station for testing."""

    def __init__(self, station_id: str, server_url: str = "ws://localhost:9000"):
        """Initialize CitrineOS simulator."""
        self.station_id = station_id
        self.server_url = server_url
        self.websocket = None
        self.connected = False
        self.message_id = 1
        self.transaction_id = None
        self.charging_state = "Available"

    async def connect(self):
        """Connect to OCPP server."""
        try:
            self.websocket = await websockets.connect(
                self.server_url, ping_interval=20, ping_timeout=10, close_timeout=10
            )
            self.connected = True
            logger.info(f"Station {self.station_id} connected to {self.server_url}")
        except Exception as e:
            logger.error(f"Failed to connect station {self.station_id}: {e}")
            raise

    async def disconnect(self):
        """Disconnect from OCPP server."""
        if self.websocket:
            await self.websocket.close()
            self.connected = False
            logger.info(f"Station {self.station_id} disconnected")

    async def send_message(self, action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Send OCPP message and wait for response."""
        if not self.connected:
            raise Exception("Not connected to server")

        message = [2, str(self.message_id), action, payload]  # OCPP 2.0.1 message type

        await self.websocket.send(json.dumps(message))
        logger.info(f"Sent {action} from {self.station_id}")

        # Wait for response
        response = await self.websocket.recv()
        response_data = json.loads(response)

        self.message_id += 1
        return response_data

    async def boot_notification(self) -> Dict[str, Any]:
        """Send BootNotification."""
        payload = {
            "chargingStation": {
                "model": "CitrineOS-Sim",
                "vendorName": "CitrineOS",
                "serialNumber": f"CSN{self.station_id}",
                "firmwareVersion": "1.0.0",
            },
            "reason": "PowerUp",
        }

        return await self.send_message("BootNotification", payload)

    async def status_notification(
        self, connector_id: int, status: str, error_code: str = "NoError"
    ) -> Dict[str, Any]:
        """Send StatusNotification."""
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "connectorStatus": status,
            "evseId": connector_id,
            "connectorId": connector_id,
            "errorCode": error_code,
        }

        return await self.send_message("StatusNotification", payload)

    async def heartbeat(self) -> Dict[str, Any]:
        """Send Heartbeat."""
        payload = {}
        return await self.send_message("Heartbeat", payload)

    async def meter_values(
        self, evse_id: int, energy: float, transaction_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Send MeterValues."""
        payload = {
            "evseId": evse_id,
            "meterValue": [
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "sampledValue": [
                        {
                            "value": str(energy),
                            "context": "Sample.Periodic",
                            "format": "Raw",
                            "measurand": "Energy.Active.Import.Register",
                            "unitOfMeasure": "kWh",
                        }
                    ],
                }
            ],
        }

        if transaction_id:
            payload["transactionId"] = transaction_id

        return await self.send_message("MeterValues", payload)

    async def transaction_event(
        self, event_type: str, transaction_id: str, trigger_reason: str = "Authorized"
    ) -> Dict[str, Any]:
        """Send TransactionEvent."""
        payload = {
            "eventType": event_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "triggerReason": trigger_reason,
            "seqNo": 1,
            "transactionInfo": {
                "transactionId": transaction_id,
                "chargingState": "Charging" if event_type == "Started" else "EVDisconnected",
            },
            "idToken": {"idToken": "AUTH123", "type": "KeyCode"},
        }

        return await self.send_message("TransactionEvent", payload)

    async def request_start_transaction(
        self, evse_id: int, id_token: str = "AUTH123"
    ) -> Dict[str, Any]:
        """Send RequestStartTransaction."""
        payload = {
            "evseId": evse_id,
            "idToken": {"idToken": id_token, "type": "KeyCode"},
            "remoteStartId": 1,
        }

        return await self.send_message("RequestStartTransaction", payload)

    async def request_stop_transaction(self, transaction_id: str) -> Dict[str, Any]:
        """Send RequestStopTransaction."""
        payload = {"transactionId": transaction_id}

        return await self.send_message("RequestStopTransaction", payload)

    async def get_variables(self, component_name: str, variable_name: str) -> Dict[str, Any]:
        """Send GetVariables."""
        payload = {
            "getVariableData": [
                {"component": {"name": component_name}, "variable": {"name": variable_name}}
            ]
        }

        return await self.send_message("GetVariables", payload)

    async def set_variables(
        self, component_name: str, variable_name: str, value: str
    ) -> Dict[str, Any]:
        """Send SetVariables."""
        payload = {
            "setVariableData": [
                {
                    "component": {"name": component_name},
                    "variable": {"name": variable_name},
                    "attributeValue": value,
                }
            ]
        }

        return await self.send_message("SetVariables", payload)

    async def set_charging_profile(
        self, evse_id: int, profile_id: int, limit: float
    ) -> Dict[str, Any]:
        """Send SetChargingProfile."""
        payload = {
            "evseId": evse_id,
            "chargingProfile": {
                "id": profile_id,
                "stackLevel": 0,
                "chargingProfilePurpose": "TxDefaultProfile",
                "chargingProfileKind": "Absolute",
                "chargingSchedule": {
                    "id": 1,
                    "chargingRateUnit": "W",
                    "chargingSchedulePeriod": [{"startPeriod": 0, "limit": limit}],
                    "duration": 3600,
                },
            },
        }

        return await self.send_message("SetChargingProfile", payload)

    async def get_charging_profiles(self, evse_id: int) -> Dict[str, Any]:
        """Send GetChargingProfiles."""
        payload = {
            "requestId": 1,
            "evseId": evse_id,
            "chargingProfilePurpose": "TxDefaultProfile",
            "stackLevel": 0,
        }

        return await self.send_message("GetChargingProfiles", payload)

    async def get_composite_schedule(self, evse_id: int, duration: int = 3600) -> Dict[str, Any]:
        """Send GetCompositeSchedule."""
        payload = {"requestId": 1, "evseId": evse_id, "duration": duration, "chargingRateUnit": "W"}

        return await self.send_message("GetCompositeSchedule", payload)

    async def clear_charging_profile(self, evse_id: int, profile_id: int) -> Dict[str, Any]:
        """Send ClearChargingProfile."""
        payload = {
            "chargingProfileId": profile_id,
            "chargingProfilePurpose": "TxDefaultProfile",
            "evseId": evse_id,
        }

        return await self.send_message("ClearChargingProfile", payload)

    async def reset(self, reset_type: str = "Immediate") -> Dict[str, Any]:
        """Send Reset."""
        payload = {"type": reset_type}

        return await self.send_message("Reset", payload)

    async def change_availability(self, operational_status: str = "Operative") -> Dict[str, Any]:
        """Send ChangeAvailability."""
        payload = {"operationalStatus": operational_status}

        return await self.send_message("ChangeAvailability", payload)

    async def trigger_message(
        self, requested_message: str, evse_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """Send TriggerMessage."""
        payload = {"requestedMessage": requested_message}

        if evse_id:
            payload["evse"] = {"id": evse_id}

        return await self.send_message("TriggerMessage", payload)

    async def unlock_connector(self, evse_id: int, connector_id: int) -> Dict[str, Any]:
        """Send UnlockConnector."""
        payload = {"evseId": evse_id, "connectorId": connector_id}

        return await self.send_message("UnlockConnector", payload)

    async def get_monitoring_report(self, evse_id: int) -> Dict[str, Any]:
        """Send GetMonitoringReport."""
        payload = {"requestId": 1, "monitoringCriteria": [], "componentVariable": []}

        return await self.send_message("GetMonitoringReport", payload)

    async def set_display_message(self, message_id: int, message: str) -> Dict[str, Any]:
        """Send SetDisplayMessage."""
        payload = {
            "message": {
                "id": message_id,
                "priority": "NormalCycle",
                "state": "Charging",
                "startDateTime": datetime.now(timezone.utc).isoformat(),
                "endDateTime": (
                    datetime.now(timezone.utc).replace(hour=23, minute=59, second=59)
                ).isoformat(),
                "message": {"format": "UTF8", "language": "en", "content": message},
            }
        }

        return await self.send_message("SetDisplayMessage", payload)

    async def clear_display_message(self, message_id: int) -> Dict[str, Any]:
        """Send ClearDisplayMessage."""
        payload = {"id": message_id}

        return await self.send_message("ClearDisplayMessage", payload)

    async def customer_information(self, customer_id: str) -> Dict[str, Any]:
        """Send CustomerInformation."""
        payload = {
            "requestId": 1,
            "customerIdentifier": customer_id,
            "idToken": {"idToken": customer_id, "type": "KeyCode"},
        }

        return await self.send_message("CustomerInformation", payload)

    async def delete_customer_information(self, customer_id: str) -> Dict[str, Any]:
        """Send DeleteCustomerInformation."""
        payload = {
            "requestId": 1,
            "customerIdentifier": customer_id,
            "idToken": {"idToken": customer_id, "type": "KeyCode"},
        }

        return await self.send_message("DeleteCustomerInformation", payload)


class CitrineOSFleetSimulator:
    """Simulates a fleet of CitrineOS charging stations."""

    def __init__(self, num_stations: int = 5, server_url: str = "ws://localhost:9000"):
        """Initialize fleet simulator."""
        self.num_stations = num_stations
        self.server_url = server_url
        self.stations = []

        # Create station simulators
        for i in range(num_stations):
            station_id = f"FLEET_STATION_{i+1:03d}"
            station = CitrineOSSimulator(station_id, server_url)
            self.stations.append(station)

    async def connect_all(self):
        """Connect all stations."""
        tasks = [station.connect() for station in self.stations]
        await asyncio.gather(*tasks)
        logger.info(f"Connected {len(self.stations)} stations")

    async def disconnect_all(self):
        """Disconnect all stations."""
        tasks = [station.disconnect() for station in self.stations]
        await asyncio.gather(*tasks)
        logger.info(f"Disconnected {len(self.stations)} stations")

    async def boot_all_stations(self):
        """Boot all stations."""
        tasks = [station.boot_notification() for station in self.stations]
        results = await asyncio.gather(*tasks)

        successful_boots = sum(1 for result in results if result[2].get("status") == "Accepted")
        logger.info(f"Successfully booted {successful_boots}/{len(self.stations)} stations")
        return results

    async def set_all_available(self):
        """Set all stations to available status."""
        tasks = []
        for i, station in enumerate(self.stations):
            task = station.status_notification(i + 1, "Available")
            tasks.append(task)

        results = await asyncio.gather(*tasks)
        logger.info(f"Set {len(self.stations)} stations to available")
        return results

    async def start_charging_sessions(self, num_sessions: int = 3):
        """Start charging sessions on multiple stations."""
        tasks = []
        for i in range(min(num_sessions, len(self.stations))):
            station = self.stations[i]
            task = station.request_start_transaction(i + 1)
            tasks.append(task)

        results = await asyncio.gather(*tasks)
        logger.info(f"Started {len(results)} charging sessions")
        return results

    async def send_meter_values(self, energy_values: list):
        """Send meter values from all stations."""
        tasks = []
        for i, station in enumerate(self.stations):
            if i < len(energy_values):
                energy = energy_values[i]
                task = station.meter_values(i + 1, energy, f"TXN{i+1:06d}")
                tasks.append(task)

        results = await asyncio.gather(*tasks)
        logger.info(f"Sent meter values from {len(results)} stations")
        return results

    async def heartbeat_all(self):
        """Send heartbeat from all stations."""
        tasks = [station.heartbeat() for station in self.stations]
        results = await asyncio.gather(*tasks)
        logger.info(f"Sent heartbeats from {len(self.stations)} stations")
        return results

    async def test_device_configuration(self):
        """Test device configuration on all stations."""
        tasks = []
        for station in self.stations:
            # Get variables
            task = station.get_variables("ChargingStation", "VendorName")
            tasks.append(task)

        results = await asyncio.gather(*tasks)
        logger.info(f"Tested device configuration on {len(self.stations)} stations")
        return results

    async def test_charging_profiles(self):
        """Test charging profile management on all stations."""
        tasks = []
        for i, station in enumerate(self.stations):
            # Set charging profile
            task = station.set_charging_profile(i + 1, 1, 22.0)
            tasks.append(task)

        results = await asyncio.gather(*tasks)
        logger.info(f"Tested charging profiles on {len(self.stations)} stations")
        return results

    async def test_monitoring(self):
        """Test monitoring on all stations."""
        tasks = []
        for station in self.stations:
            # Get monitoring report
            task = station.get_monitoring_report(1)
            tasks.append(task)

        results = await asyncio.gather(*tasks)
        logger.info(f"Tested monitoring on {len(self.stations)} stations")
        return results

    async def test_display_messages(self):
        """Test display messages on all stations."""
        tasks = []
        for i, station in enumerate(self.stations):
            # Set display message
            task = station.set_display_message(i + 1, f"Test message from station {i+1}")
            tasks.append(task)

        results = await asyncio.gather(*tasks)
        logger.info(f"Tested display messages on {len(self.stations)} stations")
        return results

    async def test_privacy_compliance(self):
        """Test privacy compliance on all stations."""
        tasks = []
        for i, station in enumerate(self.stations):
            # Customer information request
            task = station.customer_information(f"CUSTOMER{i+1:03d}")
            tasks.append(task)

        results = await asyncio.gather(*tasks)
        logger.info(f"Tested privacy compliance on {len(self.stations)} stations")
        return results


async def run_single_station_test():
    """Run test with single CitrineOS station."""
    station = CitrineOSSimulator("TEST_STATION_001")

    try:
        await station.connect()

        # Boot notification
        boot_result = await station.boot_notification()
        logger.info(f"Boot result: {boot_result}")

        # Status notification
        status_result = await station.status_notification(1, "Available")
        logger.info(f"Status result: {status_result}")

        # Heartbeat
        heartbeat_result = await station.heartbeat()
        logger.info(f"Heartbeat result: {heartbeat_result}")

        # Get variables
        vars_result = await station.get_variables("ChargingStation", "VendorName")
        logger.info(f"Get variables result: {vars_result}")

        # Set charging profile
        profile_result = await station.set_charging_profile(1, 1, 22.0)
        logger.info(f"Set charging profile result: {profile_result}")

        # Start transaction
        start_result = await station.request_start_transaction(1)
        logger.info(f"Start transaction result: {start_result}")

        # Transaction event
        if start_result[2].get("status") == "Accepted":
            transaction_id = start_result[2].get("transactionId")
            txn_result = await station.transaction_event("Started", transaction_id)
            logger.info(f"Transaction event result: {txn_result}")

            # Meter values
            meter_result = await station.meter_values(1, 22.5, transaction_id)
            logger.info(f"Meter values result: {meter_result}")

            # Stop transaction
            stop_result = await station.request_stop_transaction(transaction_id)
            logger.info(f"Stop transaction result: {stop_result}")

            # Transaction ended
            end_result = await station.transaction_event("Ended", transaction_id, "EVDisconnected")
            logger.info(f"Transaction ended result: {end_result}")

    except Exception as e:
        logger.error(f"Test failed: {e}")
    finally:
        await station.disconnect()


async def run_fleet_test():
    """Run test with fleet of CitrineOS stations."""
    fleet = CitrineOSFleetSimulator(num_stations=5)

    try:
        await fleet.connect_all()

        # Boot all stations
        await fleet.boot_all_stations()

        # Set all available
        await fleet.set_all_available()

        # Test device configuration
        await fleet.test_device_configuration()

        # Test charging profiles
        await fleet.test_charging_profiles()

        # Start charging sessions
        await fleet.start_charging_sessions(3)

        # Send meter values
        energy_values = [22.5, 18.3, 25.1, 19.7, 21.8]
        await fleet.send_meter_values(energy_values)

        # Test monitoring
        await fleet.test_monitoring()

        # Test display messages
        await fleet.test_display_messages()

        # Test privacy compliance
        await fleet.test_privacy_compliance()

        # Heartbeat all
        await fleet.heartbeat_all()

        logger.info("Fleet test completed successfully")

    except Exception as e:
        logger.error(f"Fleet test failed: {e}")
    finally:
        await fleet.disconnect_all()


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "fleet":
        asyncio.run(run_fleet_test())
    else:
        asyncio.run(run_single_station_test())
