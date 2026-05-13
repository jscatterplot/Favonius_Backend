"""Tool registry for the workflow agent.

A workflow's allowed tool set is just a list of names. The runtime
resolves each name through a :class:`ToolRegistry`, which is the single
place that maps a name to (a) the JSON schema the LLM sees and (b) the
async callable the runtime dispatches. Workflows can share tools — the
registry is global to the running process.

A tool callable is a plain ``async def fn(**kwargs) -> Any`` whose
keyword arguments match the published input schema. Anthropic's tool-use
returns the model's ``input`` as a dict; the registry calls
``fn(**input)`` after the runtime has decided dispatch is allowed and
the hard-constraint guard has approved the args.

Errors:

- :class:`ToolNotRegisteredError` — the runtime asked for a tool name
  that nobody registered. Raised by ``get`` / ``dispatch``.
- The runtime raises :class:`~src.api.agent_workflows.runtime.ToolNotAllowedError`
  (defined in ``runtime.py`` so it can be imported from the public surface)
  when the LLM tries to call a tool that *is* registered but is not in
  the workflow's allow-list. The two errors are distinct on purpose so a
  bug in the registry (forgotten registration) does not look like a
  policy violation in metrics or logs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Awaitable, Callable, Iterable

ToolCallable = Callable[..., Awaitable[Any]]


class ToolNotRegisteredError(KeyError):
    """Raised when a tool name has no callable in the registry."""


@dataclass(frozen=True)
class ToolDefinition:
    """One registered tool.

    ``input_schema`` is a JSON Schema dict in the format the Anthropic
    Messages API expects under ``tools[i].input_schema``. ``fn`` is
    invoked as ``await fn(**input)`` after the runtime's allow-list and
    constraint checks pass.
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    fn: ToolCallable
    metadata: dict[str, Any] = field(default_factory=dict)


_TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


class ToolRegistry:
    """Process-wide map of tool name → :class:`ToolDefinition`.

    Registration happens at module-import time for production tools and
    inline for tests. The registry is the SINGLE place the runtime looks
    up callables, so a test that registers a fake under the production
    name behaves exactly like production.

    Re-registering a name is a programming error — it would silently
    shadow the prior tool and is rejected with a :class:`ValueError`.
    Tests that need to swap a tool should call :meth:`unregister` first
    or construct a fresh registry.
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    # ── Registration ────────────────────────────────────────────────────

    def register(
        self,
        name: str,
        *,
        description: str,
        input_schema: dict[str, Any],
        fn: ToolCallable,
        metadata: dict[str, Any] | None = None,
    ) -> ToolDefinition:
        """Register a new tool. Raises ``ValueError`` on duplicate name."""
        if not _TOOL_NAME_RE.fullmatch(name):
            raise ValueError(
                f"invalid tool name {name!r}; expected 1-128 chars matching [a-zA-Z0-9_-]"
            )
        if name in self._tools:
            raise ValueError(f"tool {name!r} is already registered")
        defn = ToolDefinition(
            name=name,
            description=description,
            input_schema=input_schema,
            fn=fn,
            metadata=dict(metadata or {}),
        )
        self._tools[name] = defn
        return defn

    def unregister(self, name: str) -> None:
        """Drop a tool by name. No-op if absent."""
        self._tools.pop(name, None)

    # ── Lookup ──────────────────────────────────────────────────────────

    def has(self, name: str) -> bool:
        return name in self._tools

    def get(self, name: str) -> ToolDefinition:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolNotRegisteredError(name) from exc

    def names(self) -> list[str]:
        return sorted(self._tools)

    # ── Anthropic schema export ────────────────────────────────────────

    def anthropic_schemas(self, allowed: Iterable[str]) -> list[dict[str, Any]]:
        """Return the Anthropic ``tools`` array for the given allow-list.

        Order matches the order of ``allowed`` so callers get a stable,
        cache-friendly tools array across turns. Names that aren't in
        the registry surface as :class:`ToolNotRegisteredError` —
        catching that here protects every workflow from a typo in
        ``Workflow.allowed_tools``.
        """
        schemas: list[dict[str, Any]] = []
        seen: set[str] = set()
        for name in allowed:
            if name in seen:
                continue
            seen.add(name)
            defn = self.get(name)
            schemas.append(
                {
                    "name": defn.name,
                    "description": defn.description,
                    "input_schema": defn.input_schema,
                }
            )
        return schemas

    # ── Dispatch ────────────────────────────────────────────────────────

    async def dispatch(self, name: str, tool_input: dict[str, Any]) -> Any:
        """Invoke a tool by name with the LLM's input dict.

        Raises:
            ToolNotRegisteredError: If ``name`` isn't registered.
            Exception: Anything the tool callable raises (caller wraps).
        """
        defn = self.get(name)
        return await defn.fn(**(tool_input or {}))
