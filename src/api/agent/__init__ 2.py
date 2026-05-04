"""Natural-language depot chat agent.

Submodules (filled in over sprints B1-B6):

- ``plan``: Pydantic types the LLM is allowed to produce (intents, entity
  mentions, time windows). Strict validation gates anything else.
- ``auth_context``: Builds an ``AuthContext`` from a verified JWT payload,
  including the caller's ``visible_depot_ids`` from Supabase.
- ``resolve``: Server-side entity and time-window resolvers. The single place
  where names become UUIDs.
- ``llm``: Anthropic client wrapper for plan extraction and answer
  formatting (configurable via ``AGENT_LLM_MODEL``).
- ``intents``: Per-intent SQL compilers; deterministic, no LLM in the loop.
- ``audit``: ``agent_runs`` writer plus ``audit_log`` mirror.
- ``router``: FastAPI APIRouter mounted at ``/agent``.
- ``stream``: SSE event encoding.
"""
