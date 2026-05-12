"""Loader for depot-agent readiness scenarios (YAML fixtures).

Converts a fixture file into a ready-to-run :class:`StaticToolBundle`
plus the metadata the integration test needs (depot_id,
organization_id, expected block). The loader bakes in a deterministic
"now" so SoC projections are stable across runs — every departure's
``minutes_from_now`` resolves against this baseline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml

from src.core.workflows.tools import StaticToolBundle


SCENARIOS_DIR = Path(__file__).parent / "scenarios"


@dataclass
class LoadedScenario:
    scenario_id: str
    depot_id: UUID
    organization_id: UUID
    timezone: str
    window_start_local: datetime
    window_end_local: datetime
    window_start_utc: datetime
    window_end_utc: datetime
    now_utc: datetime
    tools: StaticToolBundle
    expected: dict[str, Any]
    raw: dict[str, Any]


def load_scenario(scenario_id: str, *, now_utc: datetime | None = None) -> LoadedScenario:
    """Load a YAML scenario into a :class:`LoadedScenario`.

    ``now_utc`` defaults to a fixed value (``2026-05-13T04:00Z``) so
    ``minutes_from_now`` in the YAML maps to an absolute departure
    time that is identical across runs.
    """
    path = SCENARIOS_DIR / f"{scenario_id}.yaml"
    raw = yaml.safe_load(path.read_text())
    if now_utc is None:
        # Use real now so the workflow's internal ``datetime.now(UTC)``
        # for "minutes until departure" agrees with the
        # ``minutes_from_now`` offsets in the YAML. Tests that need a
        # frozen clock pass an explicit ``now_utc``.
        now_utc = datetime.now(timezone.utc)

    departures: list[dict[str, Any]] = []
    for d in raw.get("departures", []):
        departures.append(
            {
                "vehicle_id": d["vehicle_id"],
                "route_id": d["route_id"],
                "departure_time": now_utc + timedelta(minutes=int(d["minutes_from_now"])),
                "required_soc": float(d["required_soc"]),
                "charger_id": d.get("charger_id"),
            }
        )

    tools = StaticToolBundle(
        departures=departures,
        vehicle_states={k: dict(v) for k, v in raw.get("vehicle_states", {}).items()},
        charger_states={k: dict(v) for k, v in raw.get("charger_states", {}).items()},
        charging_plans={k: dict(v) for k, v in raw.get("charging_plans", {}).items()},
        driver_assignments={k: dict(v) for k, v in raw.get("driver_assignments", {}).items()},
        alternate_chargers=[dict(a) for a in raw.get("alternate_chargers", [])],
    )

    window_start_local = datetime.fromisoformat(str(raw["window_start_local"]))
    window_end_local = datetime.fromisoformat(str(raw["window_end_local"]))

    return LoadedScenario(
        scenario_id=raw["id"],
        depot_id=UUID(str(raw["depot_id"])),
        organization_id=UUID(str(raw["organization_id"])),
        timezone=str(raw["timezone"]),
        window_start_local=window_start_local,
        window_end_local=window_end_local,
        window_start_utc=now_utc,
        window_end_utc=now_utc + timedelta(hours=4),
        now_utc=now_utc,
        tools=tools,
        expected=raw.get("expected", {}),
        raw=raw,
    )
