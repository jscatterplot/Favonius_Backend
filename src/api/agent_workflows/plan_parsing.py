"""Shared parser for an optimization run's ``schedule_json`` payload.

The optimizer writes one ``optimization_runs.schedule_json`` blob shaped as
``{"schedule": {"<vehicle_id>": {"soc": [...], "charging_power": [...]}}}``.
Two readers walk it: the daily-readiness tool
(:func:`src.api.agent_workflows.readiness_tools._make_get_charging_plan`,
which zips soc+power into a per-timestep plan) and the chat agent's readiness
intent (:func:`src.api.agent.intents.readiness.projected_soc_from_plan`, which
takes the peak SoC). Keeping the str-vs-dict / schedule / per-vehicle walk in
one place means a change to the JSON shape moves both readers together.

Pure + stdlib-only so either caller can import it without pulling heavy deps.
"""

from __future__ import annotations

import json
from typing import Any, Optional


def extract_vehicle_plan(schedule_json: Any, vehicle_id: Any) -> Optional[dict]:
    """Return one vehicle's plan dict from a run's ``schedule_json``.

    ``schedule_json`` may be a JSONB ``dict`` or a serialized ``str``
    (asyncpg leaves JSONB as text unless a codec is set). Returns the
    per-vehicle mapping (typically ``{"soc": [...], "charging_power": [...]}``)
    or ``None`` when the payload is malformed or the vehicle has no plan.
    """
    payload = schedule_json
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    schedule = payload.get("schedule")
    if not isinstance(schedule, dict):
        return None
    per_vehicle = schedule.get(str(vehicle_id))
    return per_vehicle if isinstance(per_vehicle, dict) else None
