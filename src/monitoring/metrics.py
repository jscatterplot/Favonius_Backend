"""Prometheus metrics for monitoring.

Reference: PRD_v2.md Section 10.1 (Performance Requirements)
           docs/CONTROL_LOOP.md Section "Monitoring and Metrics"
           PRD_v2.md Section 8.2 (Solver Reliability)

All metrics are prefixed with `favonius_` per CONTROL_LOOP.md.
"""

from prometheus_client import Counter, Gauge, Histogram

# Optimization metrics
# Per PRD Section 8.3: Solve time < 60 seconds for 20 vehicles
OPTIMIZATION_RUNS = Counter(
    "favonius_optimization_runs_total",
    "Total number of optimization runs",
    ["depot_id", "trigger_reason"],
)

OPTIMIZATION_DURATION = Histogram(
    "favonius_optimization_duration_seconds",
    "Optimization solve time (Gurobi wall-clock time)",
    ["depot_id"],
    buckets=[1, 5, 10, 20, 30, 45, 60],  # PRD target: < 60s
)

OPTIMIZATION_OBJECTIVE = Gauge(
    "favonius_optimization_objective_value",
    "Last optimization objective value",
    ["depot_id"],
)

OPTIMIZATION_FAILURES = Counter(
    "favonius_optimization_failures_total",
    "Total number of optimization failures",
    ["depot_id", "failure_type"],  # failure_type: 'infeasible', 'timeout', 'error'
)

# Solver reliability metrics
# Per PRD Section 8.2: Track solver usage and fallback events
SOLVER_USED = Counter(
    "favonius_solver_used_total",
    "Total number of optimizations by solver type",
    ["depot_id", "solver"],  # solver: 'gurobi' or 'highs'
)

SOLVER_FALLBACK = Counter(
    "favonius_solver_fallback_total",
    "Total number of solver fallback events",
    ["depot_id", "reason"],  # reason: 'license_failure', 'connection_error', 'solver_error'
)

# Fleet metrics
VEHICLE_SOC = Gauge(
    "favonius_vehicle_soc",
    "Vehicle state of charge (0.0-1.0)",
    ["depot_id", "vehicle_id"],
)

GRID_POWER = Gauge(
    "favonius_grid_power_kw",
    "Current grid power draw (includes building load per PRD Section 8.1)",
    ["depot_id"],
)

PEAK_DEMAND = Gauge(
    "favonius_peak_demand_kw",
    "Current month peak demand (for demand charge calculation)",
    ["depot_id"],
)

# OCPP dispatch metrics
# Per PRD Section 9.1: OCPP command dispatch tracking
OCPP_DISPATCH_SUCCESS = Counter(
    "favonius_ocpp_dispatch_success_total",
    "Total number of successful OCPP command dispatches",
    ["depot_id"],
)

OCPP_DISPATCH_FAILURES = Counter(
    "favonius_ocpp_dispatch_failures_total",
    "Total number of failed OCPP command dispatches",
    ["depot_id", "error_type"],
)

# Agent chat metrics
# Per architecture doc §8.3: four mandatory metrics for the depot chat agent.
AGENT_TURNS = Counter(
    "favonius_agent_turns_total",
    "Total depot chat agent turns by outcome and intent",
    ["status", "intent"],  # status: success|disambiguation|not_found|error
)

AGENT_TURN_DURATION = Histogram(
    "favonius_agent_turn_duration_seconds",
    "End-to-end latency per agent turn (p95 target ≤ 8 s per PRD §7)",
    ["intent"],
    buckets=[0.5, 1, 2, 4, 6, 8, 12, 20],
)

AGENT_LLM_TOKENS = Counter(
    "favonius_agent_llm_tokens_total",
    "LLM tokens consumed by the agent, split by model and direction",
    ["model", "direction"],  # direction: input | output
)

AGENT_RESOLVER_MISSES = Counter(
    "favonius_agent_resolver_misses_total",
    "Entity resolution failures by failure kind",
    ["kind"],  # kind: not_found | ambiguous
)

# Text-to-SQL agent metrics — gated by AGENT_SQL_MODE_ENABLED.
# Naming follows the favonius_agent_* family; granular labels so we can
# distinguish validator rejections from executor failures in dashboards.
AGENT_SQL_VALIDATIONS = Counter(
    "favonius_agent_sql_validations_total",
    "SQL validator outcomes for LLM-emitted SELECTs",
    ["verdict"],  # verdict: accepted | rejected:<error_kind>
)

AGENT_SQL_EXECUTIONS = Counter(
    "favonius_agent_sql_executions_total",
    "Executor outcomes after validator acceptance",
    ["outcome", "pool"],  # outcome: success|timeout|plan_error|role_error|error
)

AGENT_SQL_EXECUTION_DURATION = Histogram(
    "favonius_agent_sql_execution_seconds",
    "Wall-clock duration of one validated SELECT against agent_views.*",
    buckets=[0.05, 0.1, 0.25, 0.5, 1, 2, 5],
)

AGENT_SQL_ROWS_RETURNED = Histogram(
    "favonius_agent_sql_rows_returned",
    "Rows returned per SELECT (post-cap)",
    buckets=[0, 1, 10, 50, 100, 250, 500],
)

AGENT_SQL_TOOL_TURNS = Histogram(
    "favonius_agent_sql_tool_turns",
    "Tool-call turns per SQL-route agent turn",
    buckets=[1, 2, 3, 5, 8, 12],
)

# Depot workflow agent runtime metrics (PRD §4.3/§4.4, sprint 2).
# Naming mirrors the existing agent_search metrics one level up.
WORKFLOW_TURNS = Counter(
    "favonius_workflow_turns_total",
    "Total depot workflow agent turns by workflow, depot, and outcome",
    ["workflow", "depot", "status"],
)

WORKFLOW_TURN_DURATION = Histogram(
    "favonius_workflow_turn_duration_seconds",
    "End-to-end latency of a workflow turn (one Anthropic Messages loop)",
    ["workflow"],
    buckets=[0.5, 1, 2, 4, 8, 15, 30, 60, 120],
)

WORKFLOW_LLM_TOKENS = Counter(
    "favonius_workflow_llm_tokens_total",
    "LLM tokens consumed by the workflow agent, split by workflow, model, direction",
    ["workflow", "model", "direction"],  # direction: input | output
)

# Per-session electricity cost calculator (src/core/billing/session_cost.py).
# Migration 040 added cost_total_source; these counters mirror its value space.
SESSION_COST_COMPUTED = Counter(
    "favonius_session_cost_computed_total",
    "Charging-session cost calculations by source / outcome",
    ["source"],  # granular | fallback_average | unpriceable | no_energy | no_depot | manual | pending_close
)

SESSION_COST_COMPUTE_FAILURES = Counter(
    "favonius_session_cost_compute_failures_total",
    "Exceptions thrown in the post-commit cost task (fire-and-forget)",
    ["reason"],  # db_error | unexpected
)

SESSION_COST_DURATION = Histogram(
    "favonius_session_cost_duration_seconds",
    "Wall-clock time to compute one session's cost",
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5],
)

# Charger-side log import + reconciliation (migration 042 / charger_log_imports).
# One counter per terminal state per vendor so dashboards can isolate
# vendor-specific failure modes (e.g. ABB parse rate vs Wallbox).
CHARGER_LOG_IMPORTS = Counter(
    "favonius_charger_log_imports_total",
    "Charger-side log import lifecycle transitions",
    ["vendor", "status"],  # status: requested | received | parsed | reconciled | failed | expired
)

CHARGER_LOG_PARSE_FAILURES = Counter(
    "favonius_charger_log_parse_failures_total",
    "Parser exceptions or empty results by vendor and reason",
    ["vendor", "reason"],  # reason: corrupt_archive | parse_exception | no_entries | unsupported_vendor
)

CHARGER_LOG_RECONCILIATIONS = Counter(
    "favonius_charger_log_reconciliations_total",
    "Reconciliations written, labelled by source enum",
    ["source"],  # reconciled | partial | no_log_entries | no_session | parse_failed
)

CHARGER_LOG_UPLOAD_DURATION = Histogram(
    "favonius_charger_log_upload_duration_seconds",
    "Wall-clock time to receive and persist a charger log upload",
    buckets=[0.05, 0.1, 0.5, 1, 5, 15, 60, 300],
)

# Control loop metrics
# Per PRD Section 10.2: System availability ≥ 99.5%
CONTROL_LOOP_UPTIME = Gauge(
    "favonius_control_loop_uptime_seconds",
    "Control loop uptime in seconds",
    ["depot_id"],
)

CONTROLLER_STATE = Gauge(
    "favonius_controller_state",
    "Controller state (1=running, 0=stopped)",
    ["depot_id"],
)

CONTROLLER_MANAGER_UP = Gauge(
    "favonius_controller_manager_up",
    "1 if controller manager initialized successfully, 0 if startup failed",
)

# Solver process-pool metrics (PR: move MILP solves off the asyncio event loop).
SOLVER_POOL_INFLIGHT = Gauge(
    "favonius_solver_pool_inflight",
    "Solves currently in flight in the solver process pool (queued + executing)",
)

SOLVER_POOL_BROKEN = Counter(
    "favonius_solver_pool_broken_total",
    "Times the solver process pool became broken and was recreated",
    ["reason"],  # 'broken_pool' | 'timeout'
)

# Navirec telematics poller metrics (live SoC feed → vehicle_telemetry).
NAVIREC_POLL_CYCLES = Counter(
    "favonius_navirec_poll_cycles_total",
    "Navirec poll cycles by outcome",
    ["outcome"],  # 'ok' | 'fetch_error' | 'skipped_disabled'
)

NAVIREC_POLL_DURATION = Histogram(
    "favonius_navirec_poll_duration_seconds",
    "Wall-clock duration of a full Navirec poll cycle",
    buckets=[0.1, 0.5, 1, 2, 5, 10, 30, 60],
)

NAVIREC_READINGS_WRITTEN = Counter(
    "favonius_navirec_readings_written_total",
    "Telematics readings upserted into vehicle_telemetry",
    ["depot_id"],
)

NAVIREC_DEPOT_FAILURES = Counter(
    "favonius_navirec_depot_failures_total",
    "Per-depot poll failures (isolated; other depots continue)",
    ["depot_id"],
)

NAVIREC_LOCK_SKIPS = Counter(
    "favonius_navirec_lock_skips_total",
    "Depot writes skipped because another worker held the advisory lock",
    ["depot_id"],
)

NAVIREC_UNMATCHED_PLATES = Counter(
    "favonius_navirec_unmatched_plates_total",
    "Telematics readings whose plate matched no Favonius vehicle",
)

NAVIREC_AMBIGUOUS_PLATES = Gauge(
    "favonius_navirec_ambiguous_plates",
    "Normalized plates mapping to >1 vehicle (dropped from the resolution map)",
)

NAVIREC_STALE_READINGS = Counter(
    "favonius_navirec_stale_readings_total",
    "Readings whose device timestamp already exceeded the telemetry freshness window",
)
