"""Version metadata captured into optimization input snapshots.

The snapshot table stores ``code_version``, ``solver_version``, and
``surrogate_model_version`` so a replay months later knows exactly which
code, solver, and surrogate produced the schedule. Each helper falls
back to ``None`` rather than raising, since snapshot construction must
never break optimization.

References:
    - migration 020_snapshot_hardening.sql
    - src/core/state/readiness.py:build_snapshot
"""

from __future__ import annotations

import logging
import os
import subprocess
from functools import lru_cache
from importlib import metadata
from typing import Optional

logger = logging.getLogger(__name__)


# Surrogate model schema version. Bump when the GP feature set, kernel,
# or output transformation changes. Reference: src/core/surrogate/energy_model.py.
SURROGATE_MODEL_VERSION = "gp-v1"


@lru_cache(maxsize=1)
def get_code_version() -> Optional[str]:
    """Return a short identifier for the running code version.

    Preference order:
        1. ``GIT_SHA`` environment variable (Railway sets this)
        2. ``git rev-parse --short HEAD`` from the working tree
        3. The package ``__version__`` from pyproject.toml

    Returns ``None`` if nothing is available.
    """
    sha = os.environ.get("GIT_SHA")
    if sha:
        return sha[:12]

    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if completed.returncode == 0 and completed.stdout.strip():
            return completed.stdout.strip()
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        logger.debug("git rev-parse unavailable: %s", exc)

    try:
        return metadata.version("favonius_backend")
    except metadata.PackageNotFoundError:
        return None


@lru_cache(maxsize=1)
def get_solver_version() -> Optional[str]:
    """Return the version string of the active MILP solver.

    Reports Gurobi when its Python module is importable, else HiGHS,
    else ``None``. We do not invoke the solver here — just inspect the
    installed package version so the call is cheap.
    """
    try:
        import gurobipy

        version_parts = gurobipy.gurobi.version()  # type: ignore[attr-defined]
        return "gurobi-" + ".".join(str(part) for part in version_parts)
    except (ImportError, AttributeError, Exception) as exc:  # noqa: BLE001
        logger.debug("Gurobi version lookup skipped: %s", exc)

    try:
        return f"highs-{metadata.version('highspy')}"
    except metadata.PackageNotFoundError:
        pass

    try:
        return f"pyomo-{metadata.version('pyomo')}"
    except metadata.PackageNotFoundError:
        return None


def get_surrogate_model_version() -> str:
    """Return the surrogate energy-model version tag."""
    return SURROGATE_MODEL_VERSION
