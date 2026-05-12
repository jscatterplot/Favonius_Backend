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

# Depot-agent workflow metrics (PRD §6.1, §11.2 — sprint 6 today view).
# Counters are per (endpoint, status) so dashboards can distinguish a
# 4xx/5xx spike on one route from a slow happy path.
AGENT_WORKFLOW_REQUESTS = Counter(
    "favonius_agent_workflow_requests_total",
    "REST hits against /agent-workflows/* by endpoint and HTTP status code",
    ["endpoint", "status_code"],  # endpoint: today | today_stream | get_decision | list_decisions
)

AGENT_WORKFLOW_RUNS = Counter(
    "favonius_agent_workflow_runs_total",
    "Workflow executions by workflow_name, trigger, and final status",
    ["workflow_name", "triggered_by", "status"],
)

AGENT_WORKFLOW_RUN_DURATION = Histogram(
    "favonius_agent_workflow_run_duration_seconds",
    "Workflow run end-to-end duration (tool calls + reasoning)",
    ["workflow_name"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 20],
)

# Scheduler-specific latency: time from the planned trigger time
# (lead_time_min before earliest departure) to the moment the workflow
# completed. A consistently large value here means the scheduler is
# starting jobs late — typically because the event loop is saturated.
AGENT_WORKFLOW_SCHEDULER_LATENCY = Histogram(
    "favonius_agent_workflow_scheduler_to_completion_seconds",
    "Scheduler-trigger-to-completion latency for scheduled readiness runs",
    ["workflow_name"],
    buckets=[1, 5, 10, 30, 60, 120, 300, 600, 1800, 3600],
)

AGENT_WORKFLOW_EXCEPTIONS_OUT = Counter(
    "favonius_agent_workflow_exceptions_emitted_total",
    "Exceptions emitted by readiness runs, broken down by exception type",
    ["workflow_name", "exception_type"],
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
