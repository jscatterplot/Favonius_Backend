"""Prometheus metrics for monitoring.

Reference: Development plan Step 7.2, PRD.md#10-non-functional-requirements
"""

from prometheus_client import Counter, Gauge, Histogram

# Optimization metrics
OPTIMIZATION_RUNS = Counter(
    'favonius_optimization_runs_total',
    'Total number of optimization runs',
    ['depot_id', 'trigger_reason'],
)

OPTIMIZATION_DURATION = Histogram(
    'favonius_optimization_duration_seconds',
    'Optimization solve time',
    ['depot_id'],
    buckets=[1, 5, 10, 20, 30, 60],
)

OPTIMIZATION_OBJECTIVE = Gauge(
    'favonius_optimization_objective_value',
    'Last optimization objective value',
    ['depot_id'],
)

# Fleet metrics
VEHICLE_SOC = Gauge(
    'favonius_vehicle_soc',
    'Vehicle state of charge',
    ['depot_id', 'vehicle_id'],
)

GRID_POWER = Gauge(
    'favonius_grid_power_kw',
    'Current grid power draw',
    ['depot_id'],
)

PEAK_DEMAND = Gauge(
    'favonius_peak_demand_kw',
    'Current month peak demand',
    ['depot_id'],
)

