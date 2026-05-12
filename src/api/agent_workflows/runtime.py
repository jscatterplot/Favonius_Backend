"""Workflow agent runtime.

Given a :class:`Workflow`, a depot id, and a verified
:class:`~src.api.agent.auth_context.AuthContext`, :class:`WorkflowAgent`
runs one Anthropic Messages turn against the workflow's allow-listed
tools and writes one :class:`Decision` row. The class mirrors the
existing depot-chat orchestrator at :mod:`src.api.agent.controller` for
prompt caching, token accounting, and audit shape — workflows are a
strict superset of that flow with tool-use added.

Key invariants enforced here:

- **Allow-list before the wire.** The only tools shipped to Anthropic
  are those in ``workflow.allowed_tools`` plus the reserved
  ``submit_decision`` finaliser. The model literally cannot see the
  rest of the registry.
- **Allow-list at dispatch.** Even if the model hallucinates a tool
  name, dispatch validates against ``allowed_tools`` and raises
  :class:`ToolNotAllowedError` rather than calling whatever happened to
  be in the registry.
- **Hard constraints last.** Once the agent emits its structured
  output, :class:`HardConstraintGuard` strips actions that violate
  PRD §10.3 (departure SoC floor, ``max_grid_kw``). The agent may
  *propose* anything; the runtime decides what survives.
- **Append-only audit.** One :class:`Decision` row per turn,
  ``disposition='pending'``, with the full tool-call trace.
- **Deterministic inputs hash.** ``sha256`` of canonicalised tool calls
  (name + input + output), independent of timing fields.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Protocol
from uuid import UUID

from src.api.agent.auth_context import AuthContext
from src.api.agent_workflows.constraints import (
    DepotConstraints,
    HardConstraintGuard,
)
from src.api.agent_workflows.repo import DecisionRepo
from src.api.agent_workflows.schemas import (
    Decision,
    ToolCallRecord,
    Workflow,
)
from src.api.agent_workflows.tools import (
    Tool,
    ToolNotAllowedError,
    ToolRegistry,
)
from src.monitoring.metrics import (
    WORKFLOW_LLM_TOKENS,
    WORKFLOW_TURN_DURATION,
    WORKFLOW_TURNS,
)

logger = logging.getLogger(__name__)


# ── Reserved finaliser tool ──────────────────────────────────────────────
#
# The runtime injects a single extra tool into every workflow's Anthropic
# request: ``submit_decision``. The agent calls it exactly once, at the
# end of the turn, to hand the runtime a structured output. This avoids
# the brittle "parse the assistant's last text block as JSON" pattern —
# it gives the model a schema-checked output channel that the runtime
# can recognise without ambiguity.
#
# ``submit_decision`` is NOT part of ``workflow.allowed_tools``; it is
# always available regardless of what the workflow declares.

SUBMIT_DECISION_TOOL_NAME = "submit_decision"

_SUBMIT_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": (
                "One-paragraph plain-language summary of the decision the "
                "workflow has reached. Shown verbatim in the today view."
            ),
        },
        "proposed_actions": {
            "type": "array",
            "description": (
                "List of structured actions the workflow proposes. Each "
                "entry must have a `type` field; downstream constraint "
                "checks rely on the known kinds documented in "
                "src.api.agent_workflows.constraints.PROPOSED_ACTION_KINDS."
            ),
            "items": {"type": "object"},
        },
        "rule_applied": {
            "type": "string",
            "description": (
                "Optional name of the workflow rule that produced this "
                "decision. Used for audit / graduation analytics."
            ),
        },
    },
    "required": ["summary"],
    "additionalProperties": True,
}


def _submit_decision_tool() -> dict[str, Any]:
    """Anthropic-shaped tool definition for the finaliser."""
    return {
        "name": SUBMIT_DECISION_TOOL_NAME,
        "description": (
            "Submit the final decision for this turn. Call this exactly "
            "once when you are done reasoning. After this call the turn "
            "ends and the structured output is persisted."
        ),
        "input_schema": _SUBMIT_DECISION_SCHEMA,
    }


# ── Errors ──────────────────────────────────────────────────────────────


class DecisionLoopError(RuntimeError):
    """The agent loop terminated without a usable decision.

    Raised when the model exhausts the iteration budget without calling
    ``submit_decision``, or when an Anthropic response has no actionable
    content. The caller is expected to surface this as a generic error
    to the user; the partial run trace is still written to ``decisions``
    by the time the exception escapes — except in the iteration-budget
    case, where the runtime writes a row with an explanatory
    ``rule_applied`` and re-raises only if the caller asked it to.
    """


# ── Anthropic SDK protocol (so tests can mock without the real SDK) ─────


class AnthropicMessagesClient(Protocol):
    """Subset of ``anthropic.AsyncAnthropic`` the runtime uses."""

    @property
    def messages(self) -> Any:  # pragma: no cover - trivial passthrough
        ...


# ── Public constructor type ─────────────────────────────────────────────


# Loader signature for depot constraints. The production wiring loads
# from :class:`~src.core.models.DepotConfig`; tests inject a closure.
DepotConstraintsLoader = Callable[[UUID], Awaitable[DepotConstraints]]


@dataclass(frozen=True)
class WorkflowAgentConfig:
    """Tunable knobs for the runtime.

    Kept separate from :class:`WorkflowAgent` so tests can vary them
    without monkey-patching module state.
    """

    model: str = "claude-sonnet-4-6"
    max_tokens: int = 1024
    max_tool_iterations: int = 8


class WorkflowAgent:
    """One-shot agent loop over a workflow's allow-listed tools."""

    def __init__(
        self,
        anthropic_client: AnthropicMessagesClient,
        repo: DecisionRepo,
        depot_constraints_loader: DepotConstraintsLoader,
        *,
        guard: Optional[HardConstraintGuard] = None,
        config: Optional[WorkflowAgentConfig] = None,
    ) -> None:
        self._client = anthropic_client
        self._repo = repo
        self._load_constraints = depot_constraints_loader
        self._guard = guard or HardConstraintGuard()
        self._config = config or WorkflowAgentConfig()

    # ── Public API ──────────────────────────────────────────────────────
    async def run_turn(
        self,
        workflow: Workflow,
        depot_id: UUID,
        auth_context: AuthContext,
        tool_registry: ToolRegistry,
        user_input: Optional[dict[str, Any]] = None,
    ) -> Decision:
        """Run one workflow turn end-to-end.

        Returns the persisted :class:`Decision`. Raises
        :class:`DecisionLoopError` only on unrecoverable failures (no
        finaliser call within the iteration budget); the partial trace
        is still written.
        """
        start_ns = time.perf_counter_ns()

        # Lock in the tool surface BEFORE the first request: any name
        # the workflow lists must exist in the registry, and we'll send
        # exactly that subset (plus the finaliser) to Anthropic.
        allow_listed = tool_registry.filtered_for_workflow(workflow)
        allowed_names = {tool.name for tool in allow_listed}
        constraints = await self._load_constraints(depot_id)

        anthropic_tools: list[dict[str, Any]] = [tool.to_anthropic_tool() for tool in allow_listed]
        anthropic_tools.append(_submit_decision_tool())

        system_blocks = self._build_system_blocks(workflow)
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": self._format_user_input(user_input)}
        ]

        tool_calls: list[ToolCallRecord] = []
        submitted_output: Optional[dict[str, Any]] = None
        rule_applied: Optional[str] = None
        input_tokens = 0
        output_tokens = 0
        stop_reason: Optional[str] = None

        for _iteration in range(self._config.max_tool_iterations):
            response = await self._client.messages.create(
                model=self._config.model,
                max_tokens=self._config.max_tokens,
                system=system_blocks,
                tools=anthropic_tools,
                messages=messages,
            )
            in_tok, out_tok = _usage_tokens(response)
            input_tokens += in_tok
            output_tokens += out_tok
            WORKFLOW_LLM_TOKENS.labels(
                workflow=workflow.id,
                model=self._config.model,
                direction="input",
            ).inc(in_tok)
            WORKFLOW_LLM_TOKENS.labels(
                workflow=workflow.id,
                model=self._config.model,
                direction="output",
            ).inc(out_tok)

            stop_reason = getattr(response, "stop_reason", None)

            # Always echo the assistant turn back so any tool_use blocks
            # have a stable tool_use_id to refer to.
            messages.append({"role": "assistant", "content": response.content})

            tool_use_blocks = _extract_tool_use_blocks(response)

            if not tool_use_blocks:
                # Model finished without calling submit_decision. We are
                # done — record what we have and let the guard run.
                break

            tool_results: list[dict[str, Any]] = []
            saw_finaliser = False
            for block in tool_use_blocks:
                name = getattr(block, "name", None)
                tool_input = getattr(block, "input", None) or {}
                tool_use_id = getattr(block, "id", None) or ""

                if name == SUBMIT_DECISION_TOOL_NAME:
                    submitted_output = dict(tool_input)
                    rule_applied = submitted_output.pop("rule_applied", None)
                    saw_finaliser = True
                    # Acknowledge the finaliser so the conversation is
                    # well-formed even if the model adds another turn.
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": "decision recorded",
                        }
                    )
                    continue

                if name not in allowed_names:
                    # Defence in depth — the tools= array did NOT expose
                    # this name, so reaching this branch implies a
                    # model-side hallucination or a registry mismatch.
                    raise ToolNotAllowedError(
                        f"Workflow {workflow.id!r} attempted to call tool "
                        f"{name!r}, which is not in its allow-list."
                    )

                tool = tool_registry.get(name)
                call_record, result_content = await _dispatch_tool(tool, tool_input)
                tool_calls.append(call_record)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": result_content,
                        "is_error": call_record.error is not None,
                    }
                )

            messages.append({"role": "user", "content": tool_results})

            if saw_finaliser:
                break

        if submitted_output is None:
            # Iteration budget exhausted with no finaliser. Persist the
            # partial trace so the audit log isn't silent, then raise.
            decision = await self._persist(
                workflow=workflow,
                depot_id=depot_id,
                auth_context=auth_context,
                tool_calls=tool_calls,
                output={},
                violations=[],
                rule_applied="iteration_budget_exhausted",
                latency_ns=time.perf_counter_ns() - start_ns,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                stop_reason=stop_reason,
            )
            WORKFLOW_TURNS.labels(
                workflow=workflow.id,
                depot=str(depot_id),
                status="error",
            ).inc()
            WORKFLOW_TURN_DURATION.labels(workflow=workflow.id).observe(
                (time.perf_counter_ns() - start_ns) / 1e9
            )
            raise DecisionLoopError(
                f"Workflow {workflow.id!r} did not call "
                f"{SUBMIT_DECISION_TOOL_NAME!r} within "
                f"{self._config.max_tool_iterations} iterations "
                f"(decision_id={decision.id})."
            )

        filtered_output, violations = self._guard.filter_output(submitted_output, constraints)
        if violations and not rule_applied:
            rule_applied = "hard_constraint_violation"

        decision = await self._persist(
            workflow=workflow,
            depot_id=depot_id,
            auth_context=auth_context,
            tool_calls=tool_calls,
            output=filtered_output,
            violations=violations,
            rule_applied=rule_applied,
            latency_ns=time.perf_counter_ns() - start_ns,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            stop_reason=stop_reason,
        )

        status = "constraint_violation" if violations else "success"
        WORKFLOW_TURNS.labels(
            workflow=workflow.id,
            depot=str(depot_id),
            status=status,
        ).inc()
        WORKFLOW_TURN_DURATION.labels(workflow=workflow.id).observe(
            (time.perf_counter_ns() - start_ns) / 1e9
        )
        return decision

    # ── Internals ───────────────────────────────────────────────────────
    @staticmethod
    def _build_system_blocks(workflow: Workflow) -> list[dict[str, Any]]:
        """Wrap the workflow prompt in a cache-controlled system block.

        Marking the prompt block as ``ephemeral`` lets Anthropic's prefix
        cache serve it on repeat invocations of the same workflow. The
        block also embeds the workflow id+version so two workflows that
        happen to share prompt text never collide on the cache key —
        same pattern as :func:`src.api.agent.llm.extract_plan`.
        """
        header = (
            f"# Workflow: {workflow.name} (id={workflow.id}, "
            f"version={workflow.version}, tier={workflow.permission_tier.value})\n\n"
        )
        return [
            {
                "type": "text",
                "text": header + workflow.prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    @staticmethod
    def _format_user_input(user_input: Optional[dict[str, Any]]) -> str:
        """Render the optional caller-supplied user_input dict.

        We always send a user message — Anthropic rejects a request
        whose ``messages`` array is empty. When the caller has no
        explicit input the message instructs the agent to run the
        workflow against current state.
        """
        if not user_input:
            return (
                "Run this workflow against current depot state. "
                "When you have a decision, call `submit_decision`."
            )
        body = json.dumps(user_input, default=str, sort_keys=True)
        return (
            "Workflow input (JSON):\n"
            f"```json\n{body}\n```\n"
            "When you have a decision, call `submit_decision`."
        )

    async def _persist(
        self,
        *,
        workflow: Workflow,
        depot_id: UUID,
        auth_context: AuthContext,
        tool_calls: list[ToolCallRecord],
        output: dict[str, Any],
        violations: list[str],
        rule_applied: Optional[str],
        latency_ns: int,
        input_tokens: int,
        output_tokens: int,
        stop_reason: Optional[str],
    ) -> Decision:
        inputs_hash = _canonical_inputs_hash(tool_calls)
        decision = Decision(
            workflow_id=workflow.id,
            workflow_version=workflow.version,
            depot_id=depot_id,
            user_id=auth_context.user_id,
            organization_id=auth_context.organization_id,
            permission_tier=workflow.permission_tier,
            inputs_hash=inputs_hash,
            tool_calls=tool_calls,
            output=output,
            rule_applied=rule_applied,
            disposition="pending",
            constraint_violations=violations,
            model_id=self._config.model,
            latency_ms=max(0, latency_ns // 1_000_000),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            stop_reason=stop_reason,
        )
        await self._repo.write(decision)
        return decision


# ── Tool dispatch ───────────────────────────────────────────────────────


async def _dispatch_tool(tool: Tool, tool_input: dict[str, Any]) -> tuple[ToolCallRecord, str]:
    """Call one tool, time it, and return a record + tool_result body.

    Exceptions are caught and surfaced through the
    :attr:`ToolCallRecord.error` field rather than allowed to escape —
    a failing tool must not abort the whole turn, since the agent may
    have a recovery branch (e.g. "if telematics is offline, fall back
    to last-known SoC"). The audit row records the error verbatim.
    """
    started_ns = time.perf_counter_ns()
    try:
        result = await tool.handler(**dict(tool_input))
        elapsed_ms = (time.perf_counter_ns() - started_ns) // 1_000_000
        return (
            ToolCallRecord(
                name=tool.name,
                input=dict(tool_input),
                output=result,
                duration_ms=int(elapsed_ms),
            ),
            json.dumps(result, default=str),
        )
    except Exception as exc:  # noqa: BLE001 — tool boundary
        elapsed_ms = (time.perf_counter_ns() - started_ns) // 1_000_000
        logger.warning(
            "tool %s raised %s; surfacing to the model as an error tool_result",
            tool.name,
            exc,
        )
        return (
            ToolCallRecord(
                name=tool.name,
                input=dict(tool_input),
                output=None,
                duration_ms=int(elapsed_ms),
                error=str(exc),
            ),
            f"tool error: {exc}",
        )


# ── Helpers ─────────────────────────────────────────────────────────────


def _extract_tool_use_blocks(response: Any) -> list[Any]:
    """Return every ``tool_use`` content block on an Anthropic response."""
    out: list[Any] = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "tool_use":
            out.append(block)
    return out


def _usage_tokens(response: Any) -> tuple[int, int]:
    """Extract ``(input_tokens, output_tokens)`` from a response, defensively."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0
    return (
        int(getattr(usage, "input_tokens", 0) or 0),
        int(getattr(usage, "output_tokens", 0) or 0),
    )


def _canonical_inputs_hash(tool_calls: list[ToolCallRecord]) -> str:
    """SHA-256 of canonicalised tool calls.

    Each call's ``name`` + ``input`` + ``output`` are hashed; timing
    fields are excluded so two runs that produced the same sequence
    yield the same hash. Sorted-keys JSON canonicalisation handles
    dict ordering; the call order itself is preserved (it is part of
    the inputs).
    """
    canonical = [
        {
            "name": tc.name,
            "input": tc.input,
            "output": tc.output,
        }
        for tc in tool_calls
    ]
    blob = json.dumps(canonical, default=str, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


__all__ = [
    "DecisionLoopError",
    "SUBMIT_DECISION_TOOL_NAME",
    "WorkflowAgent",
    "WorkflowAgentConfig",
]
