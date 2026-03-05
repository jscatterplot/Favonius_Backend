#!/usr/bin/env python3
"""OCPP Charger Simulator for Testing.

Simulates multiple OCPP 1.6 charge points connecting to the server
and responding to commands.

Reference: PRD_v2.md#9-1-ocpp-integration
"""

import asyncio
import logging
import os
import random
from datetime import datetime, timezone
from typing import Optional

import websockets
from ocpp.routing import on
from ocpp.v16 import ChargePoint as CP
from ocpp.v16 import call, call_result
from ocpp.v16.enums import (
    Action,
    ChargePointStatus,
    ChargingProfileStatus,
    RegistrationStatus,
    RemoteStartStopStatus,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class SimulatedChargePoint(CP):
    """Simulated OCPP 1.6 Charge Point."""

    def __init__(self, id: str, connection, response_delay: float = 0.0):
        super().__init__(id, connection)
        self.response_delay = response_delay
        self.status = ChargePointStatus.available
        self.current_soc = 0.5
        self.current_power = 0.0
        self.transaction_id: Optional[int] = None
        self.charging_profile = None

    async def _call_with_timeout(self, request, timeout_s: float):
        """Send a CALL and enforce an upper timeout bound."""
        return await asyncio.wait_for(self.call(request), timeout=timeout_s)

    async def send_boot_notification(self, timeout_s: float = 30.0):
        """Send BootNotification to server."""
        request = call.BootNotification(
            charge_point_model="SimulatedCharger",
            charge_point_vendor="FavoniusTest",
        )
        response = await self._call_with_timeout(request, timeout_s)
        logger.info(f"[{self.id}] BootNotification response: {response.status}")
        return response.status == RegistrationStatus.accepted

    async def send_status_notification(
        self, status: ChargePointStatus = None, timeout_s: float = 30.0
    ):
        """Send StatusNotification to server."""
        if status:
            self.status = status
        request = call.StatusNotification(
            connector_id=1,
            error_code="NoError",
            status=self.status,
        )
        await self._call_with_timeout(request, timeout_s)
        logger.info(f"[{self.id}] StatusNotification sent: {self.status}")

    async def send_meter_values(self, timeout_s: float = 30.0):
        """Send MeterValues to server."""
        request = call.MeterValues(
            connector_id=1,
            meter_value=[
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "sampledValue": [
                        {
                            "value": str(int(self.current_soc * 100)),
                            "context": "Sample.Periodic",
                            "measurand": "SoC",
                            "unit": "Percent",
                        },
                        {
                            "value": str(self.current_power),
                            "context": "Sample.Periodic",
                            "measurand": "Power.Active.Import",
                            "unit": "kW",
                        },
                    ],
                }
            ],
        )
        await self._call_with_timeout(request, timeout_s)
        logger.debug(
            f"[{self.id}] MeterValues sent: SoC={self.current_soc:.2f}, Power={self.current_power}"
        )

    @on(Action.set_charging_profile)
    async def on_set_charging_profile(self, connector_id, cs_charging_profiles):
        """Handle SetChargingProfile from server."""
        if self.response_delay > 0:
            await asyncio.sleep(self.response_delay)

        self.charging_profile = cs_charging_profiles
        logger.info(f"[{self.id}] Received charging profile for connector {connector_id}")

        # Extract target power from profile
        if cs_charging_profiles and "charging_schedule" in cs_charging_profiles:
            schedule = cs_charging_profiles["charging_schedule"]
            if "charging_schedule_period" in schedule:
                periods = schedule["charging_schedule_period"]
                if periods:
                    self.current_power = periods[0].get("limit", 0.0)

        return call_result.SetChargingProfile(status=ChargingProfileStatus.accepted)

    @on(Action.remote_start_transaction)
    async def on_remote_start(self, id_tag, connector_id=1, charging_profile=None):
        """Handle RemoteStartTransaction from server."""
        if self.response_delay > 0:
            await asyncio.sleep(self.response_delay)

        self.status = ChargePointStatus.preparing
        self.transaction_id = random.randint(1000, 9999)

        if charging_profile:
            self.charging_profile = charging_profile

        logger.info(f"[{self.id}] Remote start accepted, transaction_id={self.transaction_id}")

        # Schedule status update to charging
        asyncio.create_task(self._start_charging())

        return call_result.RemoteStartTransaction(status=RemoteStartStopStatus.accepted)

    @on(Action.remote_stop_transaction)
    async def on_remote_stop(self, transaction_id):
        """Handle RemoteStopTransaction from server."""
        if self.response_delay > 0:
            await asyncio.sleep(self.response_delay)

        if transaction_id == self.transaction_id:
            self.status = ChargePointStatus.finishing
            self.current_power = 0.0
            logger.info(f"[{self.id}] Remote stop accepted")

            # Schedule status update to available
            asyncio.create_task(self._stop_charging())

            return call_result.RemoteStopTransaction(status=RemoteStartStopStatus.accepted)

        return call_result.RemoteStopTransaction(status=RemoteStartStopStatus.rejected)

    async def _start_charging(self):
        """Transition to charging state."""
        await asyncio.sleep(0.5)
        self.status = ChargePointStatus.charging
        try:
            await self.send_status_notification()
        except Exception:
            pass  # Connection may have closed before this task ran

    async def _stop_charging(self):
        """Transition to available state."""
        await asyncio.sleep(0.5)
        self.status = ChargePointStatus.available
        self.transaction_id = None
        try:
            await self.send_status_notification()
        except Exception:
            pass  # Connection may have closed before this task ran

    async def simulate_charging(self):
        """Simulate charging progression."""
        if self.status == ChargePointStatus.charging and self.current_power > 0:
            # Increase SoC based on power and time
            energy_kwh = self.current_power * (1 / 60)  # Per minute
            capacity_kwh = 324.0  # Default vehicle capacity
            soc_increase = energy_kwh / capacity_kwh
            self.current_soc = min(1.0, self.current_soc + soc_increase)


async def run_charger(
    charger_id: str,
    server_url: str,
    response_delay: float = 0.0,
    meter_interval_s: float = 60.0,
    meter_jitter_s: float = 10.0,
    call_timeout_s: float = 30.0,
):
    """Run a single simulated charger with reconnect logic."""
    backoff = 2.0
    max_backoff = 60.0

    while True:
        try:
            async with websockets.connect(
                f"{server_url}/{charger_id}",
                subprotocols=["ocpp1.6"],
                ping_interval=float(os.environ.get("WS_PING_INTERVAL", "20")),
                ping_timeout=float(os.environ.get("WS_PING_TIMEOUT", "30")),
                close_timeout=float(os.environ.get("WS_CLOSE_TIMEOUT", "10")),
                max_queue=int(os.environ.get("WS_MAX_QUEUE", "32")),
            ) as ws:
                backoff = 2.0  # Reset on successful connection
                cp = SimulatedChargePoint(charger_id, ws, response_delay)
                handler_task = asyncio.create_task(cp.start())

                try:
                    if not await cp.send_boot_notification(call_timeout_s):
                        logger.error(f"[{charger_id}] Boot rejected, will retry with backoff")
                        raise RuntimeError("BootNotification rejected")

                    await cp.send_status_notification(ChargePointStatus.available, call_timeout_s)

                    # Slightly desynchronize periodic meter updates across chargers.
                    initial_delay = random.uniform(0.0, max(1.0, meter_jitter_s))
                    await asyncio.sleep(initial_delay)

                    while True:
                        sleep_for = meter_interval_s + random.uniform(
                            -meter_jitter_s, meter_jitter_s
                        )
                        await asyncio.sleep(max(1.0, sleep_for))
                        await cp.send_meter_values(call_timeout_s)
                        await cp.simulate_charging()
                except asyncio.CancelledError:
                    logger.info(f"[{charger_id}] Shutting down")
                    raise
                finally:
                    handler_task.cancel()
                    try:
                        await handler_task
                    except asyncio.CancelledError:
                        pass
                    except websockets.exceptions.ConnectionClosed:
                        # Normal shutdown/reconnect path: peer closed.
                        pass
                    except Exception as task_exc:
                        # Prevent "Task exception was never retrieved" warnings.
                        logger.debug(f"[{charger_id}] OCPP receive loop ended: {task_exc}")

        except asyncio.CancelledError:
            return
        except Exception as e:
            retry_delay = random.uniform(backoff * 0.5, backoff * 1.5)
            logger.warning(
                f"[{charger_id}] Connection error ({type(e).__name__}): {e}. "
                f"Retrying in {retry_delay:.1f}s"
            )
            await asyncio.sleep(retry_delay)
            backoff = min(backoff * 2, max_backoff)


def _normalize_ocpp_server_url(raw: str) -> str:
    """Normalize OCPP_SERVER_URL so it is always a valid ws/wss base URL with /ocpp path.

    Accepts bare hostnames (e.g. from Railway private/public domains) and ensures
    scheme (ws for localhost, wss otherwise) and path /ocpp are present.
    """
    s = raw.strip()
    if not s:
        return "ws://localhost:9000/ocpp"
    if not s.startswith(("ws://", "wss://")):
        host = s.split("/")[0].split(":")[0]
        scheme = "ws://" if host in ("localhost", "127.0.0.1") else "wss://"
        s = scheme + s
    s = s.rstrip("/")
    if "/ocpp" not in s:
        s = s + "/ocpp"
    return s


async def main():
    """Main entry point."""
    raw_url = os.environ.get("OCPP_SERVER_URL", "ws://localhost:9000/ocpp")
    server_url = _normalize_ocpp_server_url(raw_url)
    num_chargers = int(os.environ.get("NUM_CHARGERS", "10"))
    charger_prefix = os.environ.get("CHARGER_PREFIX", "test_charger_")
    simulation_mode = os.environ.get("SIMULATION_MODE", "responsive")
    meter_interval_s = float(os.environ.get("METER_INTERVAL_SECONDS", "60"))
    meter_jitter_s = float(os.environ.get("METER_JITTER_SECONDS", "10"))
    call_timeout_s = float(os.environ.get("OCPP_CALL_TIMEOUT_SECONDS", "30"))

    # Response delay based on mode
    response_delay = 0.0 if simulation_mode == "responsive" else random.uniform(0.1, 0.5)

    logger.info(f"Starting {num_chargers} simulated chargers")
    logger.info(f"Server URL: {server_url}")
    logger.info(f"Mode: {simulation_mode}")
    logger.info(
        "Intervals: meter=%ss±%ss, call_timeout=%ss",
        meter_interval_s,
        meter_jitter_s,
        call_timeout_s,
    )

    # Wait for server to be ready
    await asyncio.sleep(5)

    # Start all chargers
    tasks = []
    for i in range(1, num_chargers + 1):
        charger_id = f"{charger_prefix}{i:02d}"
        task = asyncio.create_task(
            run_charger(
                charger_id,
                server_url,
                response_delay,
                meter_interval_s,
                meter_jitter_s,
                call_timeout_s,
            )
        )
        tasks.append(task)
        await asyncio.sleep(0.5)  # Stagger connections

    try:
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        # asyncio.run() converts SIGINT into CancelledError on the gather
        # coroutine, so we must catch both to guarantee cleanup runs.
        logger.info("Shutting down simulator")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
