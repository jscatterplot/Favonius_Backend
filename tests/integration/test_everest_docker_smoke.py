"""Opt-in Docker smoke test for backend communication with EVerest SIL."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.integration
@pytest.mark.docker
@pytest.mark.slow
def test_everest_backend_smoke() -> None:
    """Run the EVerest docker smoke script when explicitly enabled."""
    if os.getenv("RUN_EVEREST_DOCKER_TEST") != "1":
        pytest.skip("Set RUN_EVEREST_DOCKER_TEST=1 to run this docker-based smoke test.")

    if shutil.which("docker") is None:
        pytest.skip("Docker CLI not available in this environment.")

    repo_root = Path(__file__).resolve().parents[2]
    script_path = repo_root / "scripts" / "everest" / "run_backend_everest_smoke.sh"
    assert script_path.exists(), f"Expected smoke script at {script_path}"

    env = os.environ.copy()
    env.setdefault("KEEP_STACK", "0")

    completed = subprocess.run(
        [str(script_path)],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )

    if completed.returncode != 0:
        pytest.fail(
            "EVerest docker smoke test failed.\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )

