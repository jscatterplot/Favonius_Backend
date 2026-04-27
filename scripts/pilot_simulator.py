#!/usr/bin/env python3
"""Production OCPP 1.6J lifecycle simulator for pilot pre-flight checks.

Lifecycle:
connect -> BootNotification -> StatusNotification(Available) -> Authorize
-> StartTransaction -> 3x MeterValues -> wait SetChargingProfile(TxProfile, A)
-> 3x MeterValues -> StopTransaction -> disconnect
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import sys
from datetime import datetime, timezone
from typing import Optional

import websockets
from ocpp.routing import on
from ocpp.v16 import ChargePoint as CP
from ocpp.v16 import call, call_result
from ocpp.v16.enums import (
    AuthorizationStatus,
    ChargePointStatus,
    ChargingProfileStatus,
    RegistrationStatus,
)

LOG = logging.getLogger("pilot_simulator")


def _now_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


class PilotSimulator(CP):
    """Minimal charger simulator with strict assertions."""

    def __init__(self, cp_id: str, connection) -> None:
        super().__init__(cp_id, connection)
        self.tx_id: Optional[int] = None
        self.profile_received = asyncio.Event()
        self.profile_limit: Optional[float] = None
        self.profile_unit: Optional[str] = None
        self.failures: list[str] = []

    @on("SetChargingProfile")
    async def on_set_charging_profile(self, connector_id: int, cs_charging_profiles: dict, **kwargs):
        schedule = cs_charging_profiles.get("chargingSchedule", {})
        periods = schedule.get("chargingSchedulePeriod", [])
        self.profile_unit = schedule.get("chargingRateUnit")
        if periods:
            self.profile_limit = periods[0].get("limit")
        LOG.info(
            "Received SetChargingProfile connector=%s unit=%s limit=%s",
            connector_id,
            self.profile_unit,
            self.profile_limit,
        )
        if self.profile_unit != "A":
            self.failures.append(f"SetChargingProfile unit expected 'A', got {self.profile_unit}")
        self.profile_received.set()
        return call_result.SetChargingProfile(status=ChargingProfileStatus.accepted)

    async def boot(self) -> bool:
        res = await self.call(
            call.BootNotification(
                charge_point_vendor="FavoniusPilot",
                charge_point_model="PilotSimulator",
                firmware_version="1.0.0",
            )
        )
        if res.status != RegistrationStatus.accepted:
            self.failures.append(f"Boot rejected: {res.status}")
            return False
        if not isinstance(res.interval, int) or res.interval < 60:
            self.failures.append(f"Boot interval invalid: {res.interval}")
        return True

    async def status(self, connector_id: int, status: str) -> None:
        await self.call(
            call.StatusNotification(
                connector_id=connector_id,
                error_code="NoError",
                status=status,
                timestamp=_now_iso(),
            )
        )

    async def authorize(self, id_tag: str) -> bool:
        res = await self.call(call.Authorize(id_tag=id_tag))
        status = res.id_tag_info.get("status")
        if status != AuthorizationStatus.accepted:
            self.failures.append(f"Authorize not accepted: {status}")
            return False
        return True

    async def start_transaction(self, connector_id: int, id_tag: str) -> bool:
        res = await self.call(
            call.StartTransaction(
                connector_id=connector_id,
                id_tag=id_tag,
                meter_start=0,
                timestamp=_now_iso(),
            )
        )
        status = res.id_tag_info.get("status")
        if status != AuthorizationStatus.accepted:
            self.failures.append(f"StartTransaction not accepted: {status}")
            return False
        self.tx_id = res.transaction_id
        if not isinstance(self.tx_id, int) or self.tx_id <= 0:
            self.failures.append(f"Invalid transaction id: {self.tx_id}")
            return False
        return True

    async def meter_values(self, connector_id: int, energy_wh: int, power_w: float) -> None:
        await self.call(
            call.MeterValues(
                connector_id=connector_id,
                transaction_id=self.tx_id,
                meter_value=[
                    {
                        "timestamp": _now_iso(),
                        "sampledValue": [
                            {
                                "value": str(energy_wh),
                                "measurand": "Energy.Active.Import.Register",
                                "unit": "Wh",
                                "context": "Sample.Periodic",
                            },
                            {
                                "value": f"{power_w:.1f}",
                                "measurand": "Power.Active.Import",
                                "unit": "W",
                                "context": "Sample.Periodic",
                            },
                            {
                                "value": f"{power_w / 230.0:.2f}",
                                "measurand": "Current.Import",
                                "unit": "A",
                                "phase": "L1",
                                "context": "Sample.Periodic",
                            },
                        ],
                    }
                ],
            )
        )

    async def stop_transaction(self, meter_stop: int) -> None:
        await self.call(
            call.StopTransaction(
                meter_stop=meter_stop,
                timestamp=_now_iso(),
                transaction_id=self.tx_id,
                reason="Local",
            )
        )
        self.tx_id = None


async def _run(args: argparse.Namespace) -> int:
    auth_header = base64.b64encode(f"{args.user}:{args.password}".encode("utf-8")).decode("ascii")
    ws = await websockets.connect(
        args.url.rstrip("/"),
        subprotocols=["ocpp1.6"],
        extra_headers={"Authorization": f"Basic {auth_header}"},
        ping_interval=30,
        ping_timeout=20,
    )
    if ws.subprotocol != "ocpp1.6":
        LOG.error("Server did not negotiate ocpp1.6, got %r", ws.subprotocol)
        await ws.close()
        return 2

    cp = PilotSimulator(args.cp_id, ws)
    listener = asyncio.create_task(cp.start())
    try:
        if not await cp.boot():
            return 3
        await cp.status(args.connector, ChargePointStatus.available)
        if not await cp.authorize(args.id_tag):
            return 4
        if not await cp.start_transaction(args.connector, args.id_tag):
            return 5
        for i in range(3):
            await cp.meter_values(args.connector, (i + 1) * 1000, 11000.0)
            await asyncio.sleep(1.0)

        try:
            await asyncio.wait_for(cp.profile_received.wait(), timeout=args.profile_wait_s)
        except asyncio.TimeoutError:
            cp.failures.append("Timed out waiting for SetChargingProfile from CSMS")

        for i in range(3):
            await cp.meter_values(args.connector, 4000 + (i + 1) * 1000, 2300.0)
            await asyncio.sleep(1.0)
        await cp.stop_transaction(8000)
        await cp.status(args.connector, ChargePointStatus.available)
    finally:
        listener.cancel()
        try:
            await listener
        except Exception:
            pass
        await ws.close()

    if cp.failures:
        for failure in cp.failures:
            LOG.error(failure)
        return 1
    LOG.info("Lifecycle complete for %s (profile limit=%s %s)", args.cp_id, cp.profile_limit, cp.profile_unit)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Pilot OCPP lifecycle simulator")
    parser.add_argument("--url", required=True, help="wss://host/ocpp/<cp_id>")
    parser.add_argument("--cp-id", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", "--pass", dest="password", required=True)
    parser.add_argument("--connector", type=int, default=1)
    parser.add_argument("--id-tag", default="PILOT-001")
    parser.add_argument("--profile-wait-s", type=int, default=20)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())

