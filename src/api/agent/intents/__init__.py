"""Per-intent SQL compilers for the depot chat agent.

Each intent is a deterministic, hand-written compiler that turns a
:class:`~src.api.agent.plan.QueryPlan` plus resolved entities and a
resolved time window into a parameterized SQL string and parameter
list. There is no LLM in the loop here; the LLM tier produces only the
plan, and resolution to UUIDs already happened server-side before the
compiler is called.

v0 ships with a single intent (``consumption_by_user``); additional
intents land one file at a time alongside their own unit tests.
"""
