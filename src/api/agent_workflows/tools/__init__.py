"""Tool registry + per-workflow tool modules.

Importing this package as a side effect registers every tool currently
shipped: importing ``src.api.agent_workflows.tools`` populates the
global :class:`~src.api.agent_workflows.tools.registry.ToolRegistry`
with every ``@register_tool`` decorator below. Workflows then resolve
tools by name through the registry rather than importing each module
directly.
"""

from src.api.agent_workflows.tools import readiness as _readiness  # noqa: F401
from src.api.agent_workflows.tools.registry import ToolRegistry, register_tool

__all__ = ["ToolRegistry", "register_tool"]
