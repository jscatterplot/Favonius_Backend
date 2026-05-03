"""Anthropic SDK wrapper for the depot chat agent.

Two responsibilities:

1. ``extract_plan(message)`` — parse a user's natural-language message
   into a strict :class:`~src.api.agent.plan.QueryPlan` (intent +
   subjects + time window). The LLM never sees JWTs, UUIDs, or SQL —
   only the message text plus a static, cached system prompt.

2. ``format_answer(plan, resolved, window, rows)`` — turn a SQL result
   set into a natural-language reply. Non-streaming for v0; the result
   shape is small (one row per driver-day).

Model selection is environment-driven via :envvar:`AGENT_LLM_MODEL`
(default ``claude-sonnet-4-6``). The full set of generation knobs lives
in :class:`LLMConfig`; per-call overrides on ``model`` are supported so
the side-by-side eval script and tests can sweep providers without
mutating module state.

API key plumbing goes through :func:`src.security.secrets.get_secrets_manager`
so ``ANTHROPIC_API_KEY_PREVIOUS`` is honored during rotation windows
(matching the JWT secret pattern at ``src/security/secrets.py:42``).
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import anthropic
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from src.api.agent.plan import QueryPlan
from src.security.secrets import get_secrets_manager

logger = logging.getLogger(__name__)


# Models the agent is allowed to use. Switching between them is a
# redeploy of AGENT_LLM_MODEL, not a code change. New models land here
# only after they've been evaluated through scripts/agent_eval.py.
KNOWN_GOOD_MODELS: tuple[str, ...] = (
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
)

# Format step uses a non-zero temperature so the prose has some warmth;
# extraction stays at 0.0 for stability of structured output.
FORMAT_STEP_TEMPERATURE: float = 0.3

_SCHEMA_GRAPH_PATH = Path(__file__).parent / "schema_graph.yaml"


class LLMConfig(BaseModel):
    """Frozen settings loaded once at module import.

    All fields are read from environment variables with sensible
    defaults. Validation runs at module import — a typo in
    :envvar:`AGENT_LLM_MODEL` fails the process at startup, not on the
    first request. Frozen after construction so request-path code can't
    accidentally mutate it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = Field(default="claude-sonnet-4-6")
    extract_max_tokens: int = Field(default=400)
    format_max_tokens: int = Field(default=800)
    temperature: float = Field(default=0.0)
    request_timeout_s: int = Field(default=30)

    @field_validator("model")
    @classmethod
    def _model_must_be_known(cls, v: str) -> str:
        if v not in KNOWN_GOOD_MODELS:
            raise ValueError(
                f"AGENT_LLM_MODEL={v!r} is not in the known-good list "
                f"{list(KNOWN_GOOD_MODELS)}. Add it to KNOWN_GOOD_MODELS in "
                f"src/api/agent/llm.py only after evaluating via "
                f"scripts/agent_eval.py."
            )
        return v

    @classmethod
    def from_env(cls) -> "LLMConfig":
        """Build the config from environment variables."""
        return cls(
            model=os.environ.get("AGENT_LLM_MODEL", "claude-sonnet-4-6"),
            extract_max_tokens=int(os.environ.get("AGENT_LLM_EXTRACT_MAX_TOKENS", "400")),
            format_max_tokens=int(os.environ.get("AGENT_LLM_FORMAT_MAX_TOKENS", "800")),
            temperature=float(os.environ.get("AGENT_LLM_TEMPERATURE", "0.0")),
            request_timeout_s=int(os.environ.get("AGENT_LLM_TIMEOUT_S", "30")),
        )


# Loaded eagerly at import so a bad env var trips the process at startup,
# not on the first user request. Tests that need a different model
# pass it via the per-call ``model=`` override.
CONFIG: LLMConfig = LLMConfig.from_env()
logger.info(
    "agent LLM configured: model=%s extract_max_tokens=%d format_max_tokens=%d "
    "temperature=%s timeout_s=%d",
    CONFIG.model,
    CONFIG.extract_max_tokens,
    CONFIG.format_max_tokens,
    CONFIG.temperature,
    CONFIG.request_timeout_s,
)


@lru_cache(maxsize=1)
def _load_schema_graph_text() -> str:
    """Return the schema_graph.yaml contents as a string for the prompt."""
    return _SCHEMA_GRAPH_PATH.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _load_schema_graph_dict() -> dict[str, Any]:
    """Return the schema_graph.yaml contents parsed as a dict."""
    return yaml.safe_load(_load_schema_graph_text())


# ── System prompts ────────────────────────────────────────────────────────
#
# Both prompts are static across users and sessions, so they sit at the
# front of the request and the cache_control marker on each gives
# Anthropic's prefix cache a clean target. Hit rate should approach
# ~100% after warmup. See shared/prompt-caching.md in the claude-api
# skill for the invariants.


def _build_extract_plan_system_prompt() -> str:
    """Compose the extraction system prompt at import time.

    Includes the intent catalog, entity-kind list, relative-time
    vocabulary, the schema graph (so the model has stable vocabulary
    for table names), and few-shot examples covering happy path,
    ambiguity, and out-of-scope refusal cases.
    """
    schema_yaml = _load_schema_graph_text()
    return f"""\
You are the extraction stage of the Favonius depot chat agent. You convert
a user's natural-language question into a strict QueryPlan JSON object.
You DO NOT answer the user's question — a deterministic SQL compiler does
that downstream. You produce only the plan.

## Output contract

You MUST return JSON matching this Pydantic schema (use the
``emit_query_plan`` tool):

```
intent: "consumption_by_user"           # the only v0 intent
subjects: list[
    {{ kind: "driver"|"vehicle"|"depot"|"rfid", text: str }}
]
time_window:
    {{ kind: "relative", relative: <one of last_month, this_month,
                                       last_week, this_week, today,
                                       yesterday> }}
  | {{ kind: "absolute", from_iso: "YYYY-MM-DD", to_iso: "YYYY-MM-DD" }}
group_by: list[ "driver" | "depot" | "day" | "month" | "category" ]   # optional
```

## Entity kinds

- driver  — a person, often referred to by first name, full name, or an
            employee/badge ID. Examples: "John", "Jane Doe", "EMP-1234".
- vehicle — a bus or van, referred to by license plate, VIN suffix, or
            a customer label. Examples: "bus 42", "EV-007".
- depot   — a physical site. Examples: "Vilnius depot", "the main yard".
- rfid    — an RFID card by tag or label. Use only when the user
            explicitly mentions a card/tag/badge.

## Time vocabulary

Prefer relative phrases when the user's wording is relative
("last month", "this week", "yesterday"). Use absolute when the user
gives explicit dates ("from April 1 to April 30").

## Schema graph (for vocabulary, NOT for SQL)

You never produce SQL. The graph is here so you use the same words the
backend uses (e.g. "driver" not "user", "depot" not "yard"):

```yaml
{schema_yaml}
```

## Refusal / out-of-scope

If the message asks for ANY of the following, return ``subjects: []``
(an empty list) so the server returns a refusal to the user:

- Anything that would write or change state ("schedule", "assign",
  "update", "send", "create", "delete").
- More than one intent in a single turn ("how much did John charge AND
  list his sessions").
- A non-English message (we are English-only at v0).
- A question outside the consumption_by_user intent (vehicle status,
  tariff projections, charger health, optimization runs, etc.).

When refusing, still pick a syntactically valid time_window
(``kind="relative", relative="this_month"`` is fine) — the empty
``subjects`` array is the signal.

## Few-shot examples

Example 1 — happy path, single driver, relative time:
  USER: How much did John charge last month?
  PLAN: {{
    "intent": "consumption_by_user",
    "subjects": [{{ "kind": "driver", "text": "John" }}],
    "time_window": {{ "kind": "relative", "relative": "last_month" }},
    "group_by": []
  }}

Example 2 — happy path, multiple drivers, absolute dates:
  USER: Show me consumption for Jane Doe and EMP-1234 from 2026-04-01
        to 2026-04-30, grouped by day.
  PLAN: {{
    "intent": "consumption_by_user",
    "subjects": [
      {{ "kind": "driver", "text": "Jane Doe" }},
      {{ "kind": "driver", "text": "EMP-1234" }}
    ],
    "time_window": {{
      "kind": "absolute",
      "from_iso": "2026-04-01",
      "to_iso": "2026-04-30"
    }},
    "group_by": ["day"]
  }}

Example 3 — ambiguity (two Johns) — extract both as written, the
server-side resolver handles ambiguity:
  USER: How much did the two Johns charge yesterday?
  PLAN: {{
    "intent": "consumption_by_user",
    "subjects": [{{ "kind": "driver", "text": "John" }}],
    "time_window": {{ "kind": "relative", "relative": "yesterday" }},
    "group_by": []
  }}

Example 4 — refusal (write intent):
  USER: Schedule John for a charge tomorrow morning.
  PLAN: {{
    "intent": "consumption_by_user",
    "subjects": [],
    "time_window": {{ "kind": "relative", "relative": "this_month" }},
    "group_by": []
  }}

Example 5 — refusal (out-of-scope intent):
  USER: What's the current SoC of bus 42?
  PLAN: {{
    "intent": "consumption_by_user",
    "subjects": [],
    "time_window": {{ "kind": "relative", "relative": "this_month" }},
    "group_by": []
  }}
"""


def _build_format_answer_system_prompt() -> str:
    """Compose the formatter system prompt.

    Style guide: friendly, concise, never reveal raw IDs (UUIDs are
    server-internal). The formatter receives a serialized summary of the
    SQL result plus the user's original question.
    """
    return """\
You are the answer-formatter for the Favonius depot chat agent. You
receive a structured summary of a SQL result set plus the user's
original question, and produce a friendly, concise natural-language
reply.

## Style

- Friendly but professional. Sound like a colleague, not a chatbot.
- Concise — lead with the answer, then context. No throat-clearing.
- Use the canonical display names provided ("John Smith (Vilnius)"),
  never raw UUIDs.
- Energy in kWh with one decimal place. Cost with the currency symbol
  the depot uses.
- If the result is empty, say so plainly and suggest the most likely
  reason ("no charging sessions in that window").
- If some sessions had no assigned driver but matched on RFID card,
  surface that as a footnote: "N sessions on cards not currently
  assigned to this driver."
- Never speculate about data you weren't given.
"""


# Built once at import; embedded into every request so the prompt cache
# can serve them. cache_control markers are added at call time.
EXTRACT_PLAN_SYSTEM_PROMPT: str = _build_extract_plan_system_prompt()
FORMAT_ANSWER_SYSTEM_PROMPT: str = _build_format_answer_system_prompt()


# ── Anthropic client (singleton, rotation-aware) ──────────────────────────


_client: Optional[anthropic.AsyncAnthropic] = None


def _get_client() -> anthropic.AsyncAnthropic:
    """Return a singleton :class:`anthropic.AsyncAnthropic`.

    The API key is fetched via the existing rotation-aware secrets
    manager. ``ANTHROPIC_API_KEY_PREVIOUS`` is honored automatically by
    callers that need to verify against multiple keys (matching the JWT
    pattern) — for outbound calls we use the *current* key, which is
    the first entry returned by ``get_rotation_secrets``.

    Constructs the client with ``timeout=LLMConfig.request_timeout_s``
    so a stuck network never blocks the request path indefinitely.
    """
    global _client
    if _client is not None:
        return _client

    manager = get_secrets_manager()
    # ``get_rotation_secrets`` returns [current, previous?] — we sign
    # outbound requests with the current key.
    api_keys = manager.get_rotation_secrets("ANTHROPIC_API_KEY")
    api_key = api_keys[0]

    _client = anthropic.AsyncAnthropic(
        api_key=api_key,
        timeout=float(CONFIG.request_timeout_s),
    )
    return _client


def _reset_client_for_tests() -> None:
    """Drop the cached client so tests can patch ``AsyncAnthropic`` cleanly."""
    global _client
    _client = None


# ── Plan extraction ────────────────────────────────────────────────────────


# Anthropic's tool_use is the SDK's structured-output channel: the model
# is forced to emit a tool call whose ``input`` validates against the
# schema, and we round-trip that input through Pydantic. Defined from
# ``QueryPlan.model_json_schema()`` so the schema and the validator
# never diverge.
_QUERY_PLAN_TOOL_NAME = "emit_query_plan"


def _query_plan_tool() -> dict[str, Any]:
    """Build the tool definition for structured QueryPlan emission.

    Uses ``model_json_schema()`` so the LLM-facing schema and the
    server-side Pydantic validator are derived from the same source.
    """
    schema = QueryPlan.model_json_schema()
    return {
        "name": _QUERY_PLAN_TOOL_NAME,
        "description": (
            "Emit the structured QueryPlan extracted from the user's message. "
            "Always call this tool exactly once — never reply with prose."
        ),
        "input_schema": schema,
    }


class LLMExtractionError(RuntimeError):
    """Raised when the LLM produces output we cannot validate.

    We retry once on validation failure with a "your last output was
    malformed, here is the schema again" follow-up. A second failure
    raises this exception — the caller surfaces a generic error to the
    user and writes ``status='error'`` to ``agent_runs``.
    """


def _extract_tool_input(message: Any) -> Optional[dict[str, Any]]:
    """Pull the ``emit_query_plan`` tool input from an Anthropic response.

    Returns ``None`` if the model did not call the tool (e.g. emitted
    only a text block) — the caller treats that as a validation failure
    and may retry.
    """
    for block in message.content:
        # SDK content blocks expose ``.type``; guard with getattr so a
        # mock that returns plain dicts also works.
        block_type = getattr(block, "type", None)
        if block_type == "tool_use" and getattr(block, "name", None) == _QUERY_PLAN_TOOL_NAME:
            tool_input = getattr(block, "input", None)
            if isinstance(tool_input, dict):
                return tool_input
    return None


async def extract_plan(message: str, *, model: Optional[str] = None) -> QueryPlan:
    """Extract a :class:`QueryPlan` from a natural-language message.

    Args:
        message: The user's raw message text.
        model: Optional per-call model override. Used by the
            side-by-side eval script and tests; production code leaves
            it unset and inherits ``CONFIG.model``.

    Returns:
        A validated :class:`QueryPlan`.

    Raises:
        LLMExtractionError: If the model fails to emit valid JSON twice
            in a row. The caller is responsible for surfacing a generic
            failure to the user.
    """
    chosen_model = model or CONFIG.model
    client = _get_client()

    system: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": EXTRACT_PLAN_SYSTEM_PROMPT,
            # Cache the system prompt so repeated calls hit the prefix
            # cache. ephemeral = 5-minute TTL, ample for the volume of
            # depot-chat traffic at pilot scale.
            "cache_control": {"type": "ephemeral"},
        }
    ]
    tools = [_query_plan_tool()]

    messages: list[dict[str, Any]] = [{"role": "user", "content": message}]

    # First attempt.
    response = await client.messages.create(
        model=chosen_model,
        max_tokens=CONFIG.extract_max_tokens,
        temperature=CONFIG.temperature,
        system=system,
        tools=tools,
        tool_choice={"type": "tool", "name": _QUERY_PLAN_TOOL_NAME},
        messages=messages,
    )

    tool_input = _extract_tool_input(response)
    if tool_input is not None:
        try:
            return QueryPlan.model_validate(tool_input)
        except ValidationError as exc:
            first_error = str(exc)
            logger.warning(
                "agent extract_plan: first attempt failed Pydantic validation; retrying once. error=%s",
                first_error,
            )
    else:
        first_error = "model did not call emit_query_plan tool"
        logger.warning("agent extract_plan: first attempt did not call tool; retrying once.")

    # Retry once with a corrective follow-up. We keep the original user
    # message at the top of the conversation so the model still has full
    # context, then add the failed assistant response and a corrective
    # user message that names the validation error.
    schema_json = json.dumps(QueryPlan.model_json_schema(), indent=2)
    retry_messages: list[dict[str, Any]] = [
        {"role": "user", "content": message},
        {"role": "assistant", "content": response.content},
        {
            "role": "user",
            "content": (
                "Your last output was malformed and failed validation. "
                f"Validation error:\n{first_error}\n\n"
                "Here is the QueryPlan JSON schema again — call the "
                f"`{_QUERY_PLAN_TOOL_NAME}` tool exactly once with input "
                "that matches it:\n"
                f"{schema_json}"
            ),
        },
    ]

    retry_response = await client.messages.create(
        model=chosen_model,
        max_tokens=CONFIG.extract_max_tokens,
        temperature=CONFIG.temperature,
        system=system,
        tools=tools,
        tool_choice={"type": "tool", "name": _QUERY_PLAN_TOOL_NAME},
        messages=retry_messages,
    )

    retry_input = _extract_tool_input(retry_response)
    if retry_input is None:
        raise LLMExtractionError("LLM did not call emit_query_plan tool on retry; aborting turn.")
    try:
        return QueryPlan.model_validate(retry_input)
    except ValidationError as exc:
        raise LLMExtractionError(f"LLM produced invalid QueryPlan twice in a row: {exc}") from exc


# ── Answer formatting ──────────────────────────────────────────────────────


def _summarize_rows_for_format(rows: list[dict[str, Any]]) -> str:
    """Compact, deterministic JSON summary of the SQL rows for the model.

    Keeps the payload bounded so a runaway query doesn't blow the
    formatter's max_tokens budget. The formatter has a known small
    output shape (one row per driver-day for v0).
    """
    return json.dumps(rows, default=str, sort_keys=True)


async def format_answer(
    plan: QueryPlan,
    resolved: list[dict[str, Any]],
    window: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    model: Optional[str] = None,
) -> str:
    """Format a SQL result set into a natural-language reply.

    Non-streaming for v0 — the result shape is bounded.

    Args:
        plan: The extracted :class:`QueryPlan` from the user's message
            (gives the model the original intent + grouping).
        resolved: Server-side resolution output, serialized to plain
            dicts (display names, organization scoping). The full
            :class:`~src.api.agent.auth_context.ResolvedEntity` is
            converted via ``.model_dump()`` upstream.
        window: Resolved time window — start/end UTC strings + tz.
        rows: SQL result rows, serialized to plain dicts.
        model: Optional per-call model override.

    Returns:
        A natural-language reply string.
    """
    chosen_model = model or CONFIG.model
    client = _get_client()

    payload = {
        "user_intent": plan.intent,
        "user_group_by": plan.group_by,
        "resolved_subjects": resolved,
        "time_window": window,
        "row_count": len(rows),
        "rows": rows,
    }
    user_message = (
        "Format the following result set into a friendly, concise reply. "
        "The user's original question is implied by `user_intent` and "
        "`resolved_subjects`. Do not invent rows that aren't here.\n\n"
        f"```json\n{_summarize_rows_for_format([payload])}\n```"
    )

    system: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": FORMAT_ANSWER_SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    response = await client.messages.create(
        model=chosen_model,
        max_tokens=CONFIG.format_max_tokens,
        temperature=FORMAT_STEP_TEMPERATURE,
        system=system,
        messages=[{"role": "user", "content": user_message}],
    )

    parts: list[str] = []
    for block in response.content:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            parts.append(getattr(block, "text", ""))
    return "".join(parts).strip()
