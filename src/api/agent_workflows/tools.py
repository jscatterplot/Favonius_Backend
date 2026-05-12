"""Tool registry for the workflow runtime.

Tools are pure async callables registered once at import time. The
registry is the single place the runtime resolves a tool name to a
callable + schema. Workflows reference tools by name; the runtime
filters the registry down to ``workflow.allowed_tools`` before handing
the schema list to Anthropic so the model never *sees* a tool it isn't
permitted to call (defence in depth; the dispatch path still validates
on every tool_use block in case a future code path widens the
``tools=`` parameter without our knowledge).

A note on safety: tool functions are trusted code we ship; tool *names*
are not. Workflow definitions are data — they may eventually be loaded
from configuration — so the registry treats any reference to an
unknown tool as a hard failure (see :class:`ToolNotRegisteredError`).
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from src.api.agent_workflows.schemas import Workflow

ToolHandler = Callable[..., Awaitable[Any]]


class ToolNotRegisteredError(KeyError):
    """Raised when a workflow references a tool that isn't in the registry."""


class ToolNotAllowedError(PermissionError):
    """Raised when a dispatched call names a tool outside ``allowed_tools``.

    This represents a programming or model-output bug — the runtime
    constrains the Anthropic ``tools=`` array to the allow-list, so the
    only way this fires in practice is if the LLM hallucinates a tool
    name. Treated as a hard failure, never silently dropped.
    """


@dataclass(frozen=True)
class Tool:
    """One registered tool.

    ``input_schema`` is the JSON Schema Anthropic's tool_use channel
    expects. ``handler`` is a coroutine function whose keyword arguments
    must match the schema's properties.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler

    def to_anthropic_tool(self) -> dict[str, Any]:
        """Render in the shape Anthropic's Messages API accepts."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class ToolRegistry:
    """Mutable map of tool name → :class:`Tool`.

    Registration happens at import time of whatever module owns a tool;
    the registry is then handed to :class:`WorkflowAgent` per-request.
    Workflows never mutate it.
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    # ── Registration ─────────────────────────────────────────────────────
    def register(self, tool: Tool) -> None:
        """Add a tool. Duplicate names raise ``ValueError``."""
        if not inspect.iscoroutinefunction(tool.handler):
            raise TypeError(
                f"Tool {tool.name!r} handler must be an async function "
                f"(got {type(tool.handler).__name__})."
            )
        if tool.name in self._tools:
            raise ValueError(f"Tool {tool.name!r} is already registered.")
        self._tools[tool.name] = tool

    def register_function(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        handler: ToolHandler,
    ) -> Tool:
        """Convenience overload that builds the :class:`Tool` for you."""
        tool = Tool(
            name=name,
            description=description,
            input_schema=input_schema,
            handler=handler,
        )
        self.register(tool)
        return tool

    # ── Lookup ───────────────────────────────────────────────────────────
    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotRegisteredError(name) from exc

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._tools

    def names(self) -> set[str]:
        return set(self._tools)

    # ── Workflow scoping ────────────────────────────────────────────────
    def filtered_for_workflow(self, workflow: Workflow) -> list[Tool]:
        """Return the subset of tools a workflow may invoke.

        Order follows ``workflow.allowed_tools`` so the Anthropic
        request body is deterministic — this keeps the prompt-cache
        prefix stable across calls. Unknown names raise
        :class:`ToolNotRegisteredError` so misconfigurations fail loud
        at construction time rather than on the first user request.
        """
        seen: set[str] = set()
        out: list[Tool] = []
        for name in workflow.allowed_tools:
            if name in seen:
                raise ValueError(f"Workflow {workflow.id!r} lists tool {name!r} more than once.")
            seen.add(name)
            out.append(self.get(name))
        return out


__all__ = [
    "Tool",
    "ToolHandler",
    "ToolNotAllowedError",
    "ToolNotRegisteredError",
    "ToolRegistry",
]
