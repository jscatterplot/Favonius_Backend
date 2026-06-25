"""Trivial worker functions used only by SolverPool unit tests.

Kept importable as ``tests.unit.solver_pool_workers`` so ``spawn``ed child
processes can unpickle the worker callables. Not used by production code.
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


def sleep_echo_worker(state: Any = 0.0, *_args: Any, **_kwargs: Any) -> dict:
    """Sleep ``state`` seconds (occupying the worker slot), then echo.

    Used to test that a solve queued behind a long one still gets its full
    parent timeout once it actually starts executing.
    """
    time.sleep(float(state))
    return {"slept": float(state), "pid": os.getpid()}
