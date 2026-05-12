"""Typed tool registry for depot-agent workflows.

A "tool" is an async function the workflow prompt is permitted to call.
The registry is a thin name-to-callable lookup so a workflow can resolve
its allowlist (``Workflow.allowed_tools`` in the PRD §5.2 dataclass)
without each workflow having to import every module.

Design notes
------------
- Registration is a side effect of importing the tool module — the
  ``@register_tool("name")`` decorator both names the tool and inserts
  it into a process-global :class:`ToolRegistry`. Tests should call
  :func:`ToolRegistry.snapshot` if they need an isolated copy.
- Registration is idempotent: re-registering the same name with the
  same callable is a no-op (so re-importing during a hot-reload
  doesn't raise). Re-registering with a *different* callable raises
  ``ValueError`` — that almost always indicates a name collision a
  developer should fix, not paper over.
- The registry holds the raw callables; it does not own argument
  validation, auth, or audit. Workflow orchestrators are responsible
  for those concerns when they invoke the callables — exactly the same
  separation the chat agent enforces between resolver and compiler.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

# Tools are async functions returning JSON-serialisable values. The
# concrete signatures live in the tool modules — we type the registry
# as ``Callable[..., Awaitable[Any]]`` so a single registry covers
# every workflow.
Tool = Callable[..., Awaitable[Any]]


class ToolRegistry:
    """Registry mapping tool names to their async callables."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, name: str, tool: Tool) -> None:
        """Register ``tool`` under ``name``.

        Idempotent for identical re-registration. Conflicting
        re-registration (same name, different callable) raises
        ``ValueError`` because it almost always points at a real
        bug (two tools fighting over a name).
        """
        existing = self._tools.get(name)
        if existing is None:
            self._tools[name] = tool
            return
        if existing is tool:
            return
        raise ValueError(f"tool name {name!r} is already registered to a different callable")

    def get(self, name: str) -> Tool:
        """Return the callable registered under ``name``.

        Raises ``KeyError`` with a stable message so workflow orchestrators
        can surface "unknown tool" cleanly in their audit trace.
        """
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"no tool registered under name {name!r}") from exc

    def names(self) -> list[str]:
        """Return the sorted list of registered tool names."""
        return sorted(self._tools)

    def snapshot(self) -> dict[str, Tool]:
        """Return a shallow copy of the registry contents.

        Used by tests that want to assert "exactly these tools are
        registered" without mutating the process-global registry.
        """
        return dict(self._tools)


# Process-global registry. The decorator below pushes every tool into
# this instance at import time. Workflow orchestrators read from it via
# :func:`get_registry`.
_REGISTRY = ToolRegistry()


def register_tool(name: str) -> Callable[[Tool], Tool]:
    """Decorator: register ``func`` as the tool named ``name``.

    Example::

        @register_tool("get_vehicle_state")
        async def get_vehicle_state(...): ...
    """

    def _decorator(func: Tool) -> Tool:
        _REGISTRY.register(name, func)
        return func

    return _decorator


def get_registry() -> ToolRegistry:
    """Return the process-global :class:`ToolRegistry`."""
    return _REGISTRY
