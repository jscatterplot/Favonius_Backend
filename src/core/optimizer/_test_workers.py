"""Trivial worker functions used only by SolverPool unit tests.

Living under ``src/`` keeps them importable by ``spawn``ed child processes
regardless of how pytest sets up the test package path. They are *not* used
by production code; the real worker is ``pool._worker_solve``.
"""

from __future__ import annotations

import os
import time
from typing import Any


def echo_worker(*args: Any, **kwargs: Any) -> dict:
    """Return the call signature so the test can assert payload integrity."""
    return {"args": args, "kwargs": kwargs, "pid": os.getpid()}


def crash_worker(*_args: Any, **_kwargs: Any) -> dict:
    """Exit the worker process immediately to provoke BrokenProcessPool."""
    os._exit(1)


def slow_worker(*_args: Any, sleep_s: float = 5.0, **_kwargs: Any) -> dict:
    """Sleep past the parent timeout so the wait_for fires."""
    time.sleep(sleep_s)
    return {"slept": sleep_s}
