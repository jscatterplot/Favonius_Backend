"""Depot-agent workflow tooling — sprint 4+ readiness substrate.

Holds the typed tool registry and per-workflow tool modules. Tools are
the only contract between a workflow prompt and the database; they
enforce tenant scoping (``AuthContext.visible_depot_ids``) and return
JSON-serialisable dictionaries the LLM can reason about.

This package is intentionally separate from ``src.api.agent``: that one
hosts the chat-style "ask a question" agent (single SQL intent per
turn), while ``agent_workflows`` hosts deterministic multi-step
workflows (today: daily readiness). The two share the
:class:`~src.api.agent.auth_context.AuthContext` shape and the same
visible-depot scoping rule, but their wire protocols and runtime
semantics diverge.
"""

from src.api.agent_workflows.tools.registry import (
    ToolRegistry,
    get_registry,
    register_tool,
)

__all__ = ["ToolRegistry", "get_registry", "register_tool"]
