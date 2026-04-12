"""Interpreter startup compatibility patches for test environments.

This project targets newer dependency stacks than some CI/local test images.
These shims keep imports working during test collection when optional packages
or symbols are unavailable.
"""

from typing import Any

import pydantic
import pydantic.main as pydantic_main


if not hasattr(pydantic_main, "IncEx"):
    pydantic_main.IncEx = Any  # type: ignore[attr-defined]


if not hasattr(pydantic, "with_config"):
    def with_config(config: Any):
        """Minimal compatibility decorator used by realtime/supabase."""

        def decorator(cls: Any) -> Any:
            cls.model_config = config
            return cls

        return decorator

    pydantic.with_config = with_config  # type: ignore[attr-defined]
