"""Environment-driven configuration for the traffic-fine triage agent.

Mirrors the feature-flag style of ``src/api/agent_workflows/feature_flag.py``
and the data-sources flags: plain environment reads with safe defaults so the
agent can be toggled without a code change. The agent is OFF by default.
"""

from __future__ import annotations

import os

DEFAULT_ALERT_WINDOW_HOURS = 48.0
DEFAULT_UPLOAD_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB
DEFAULT_SWEEP_INTERVAL_S = 900  # 15 minutes


def is_traffic_fine_agent_enabled() -> bool:
    """True when ``TRAFFIC_FINE_AGENT_ENABLED`` is set truthy (default False)."""
    return os.environ.get("TRAFFIC_FINE_AGENT_ENABLED", "false").strip().lower() == "true"


def alert_window_hours() -> float:
    """Hours-before-deadline threshold that triggers an alert (default 48)."""
    raw = os.environ.get("TRAFFIC_FINE_ALERT_WINDOW_HOURS")
    if not raw:
        return DEFAULT_ALERT_WINDOW_HOURS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_ALERT_WINDOW_HOURS
    return value if value > 0 else DEFAULT_ALERT_WINDOW_HOURS


def upload_max_bytes() -> int:
    """Per-upload byte cap for fine documents (default 10 MiB)."""
    raw = os.environ.get("TRAFFIC_FINE_UPLOAD_MAX_BYTES")
    if not raw:
        return DEFAULT_UPLOAD_MAX_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_UPLOAD_MAX_BYTES
    return value if value > 0 else DEFAULT_UPLOAD_MAX_BYTES


def sweep_interval_s() -> int:
    """Seconds between deadline re-check sweeps (default 900)."""
    raw = os.environ.get("TRAFFIC_FINE_SWEEP_INTERVAL_S")
    if not raw:
        return DEFAULT_SWEEP_INTERVAL_S
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_SWEEP_INTERVAL_S
    return value if value > 0 else DEFAULT_SWEEP_INTERVAL_S
