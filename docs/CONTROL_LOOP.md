# Control Loop Documentation

## Overview

The control loop is the core orchestration system for depot charging optimization. It automatically runs optimizations, monitors for re-optimization triggers, and dispatches charging commands to chargers via OCPP.

**Product direction:** [PRD_Depot_Agent.md](PRD_Depot_Agent.md). The control loop is part of the substrate the depot agent builds on.

## Architecture

### Components

1. **DepotController**: Manages optimization and command dispatch for a single depot
2. **ControllerManager**: Manages multiple DepotController instances across all depots
3. **TriggerMonitor**: Monitors conditions that trigger re-optimization (SoC deviation, price changes, etc.)
4. **OCPPServer**: Handles OCPP communication with chargers

### Data Flow

```
Application Startup
    ↓
ControllerManager.start_all_controllers()
    ↓
For each depot:
    - Load depot configuration
    - Create DepotController
    - Start control loop (background task)
    ↓
Control Loop (per depot):
    - Hourly optimization (24/7)
    - Trigger-based re-optimization
    - OCPP command dispatch
    ↓
TriggerMonitor (background):
    - Monitor vehicle SoC
    - Monitor electricity prices
    - Trigger re-optimization when thresholds exceeded
```

## Configuration

### ControllerConfig

Configuration is loaded from environment variables with defaults:

| Variable | Default | Description |
|----------|---------|-------------|
| `FAVONIUS_OPTIMIZATION_HORIZON_HOURS` | 24 | Optimization horizon in hours |
| `FAVONIUS_HOURLY_OPT_START` | 7 | Hourly optimization start hour (0-23) |
| `FAVONIUS_HOURLY_OPT_END` | 23 | Hourly optimization end hour (0-23) |
| `FAVONIUS_OPTIMIZATION_TIMEOUT` | 60.0 | Solver timeout in seconds |
| `FAVONIUS_TRIGGER_COOLDOWN_MIN` | 5 | Cooldown after trigger in minutes |
| `FAVONIUS_MAX_OPT_FAILURES` | 3 | Max failures before circuit break |
| `FAVONIUS_DISPATCH_RETRIES` | 3 | OCPP dispatch retry attempts |
| `FAVONIUS_DISPATCH_RETRY_DELAY` | 2.0 | Delay between retries in seconds |
| `FAVONIUS_SHUTDOWN_TIMEOUT` | 30.0 | Graceful shutdown timeout in seconds |

### OCPP Server Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `OCPP_SERVER_ENABLED` | false | Enable OCPP server |
| `OCPP_SERVER_HOST` | 0.0.0.0 | OCPP server host |
| `OCPP_SERVER_PORT` | 9000 | OCPP server port |

## Control Loop Operation

### Hourly Optimization

Controllers run hourly optimizations during active hours (default: 24/7):

1. Assemble current depot state (vehicle SoCs, prices, etc.)
2. Build and solve optimization model
3. Store result in database
4. Dispatch charging commands to chargers
5. Update trigger monitor expected state

### Trigger-Based Re-Optimization

The TriggerMonitor continuously monitors SoC deviation, price changes, return-time delays, inter-depot handoffs, and the scheduled hourly tick. See the re-optimization trigger table (detection methods and thresholds) in [ARCHITECTURE.md](ARCHITECTURE.md).

When a trigger fires:
1. Check cooldown period (prevents rapid re-optimization)
2. Check circuit breaker (prevents repeated failures)
3. Run optimization with trigger reason logged
4. Update expected state for monitoring

### OCPP Command Dispatch

After optimization completes:

1. Get vehicle-to-charger mapping from database
2. Build charging profile for next 4 hours (16 x 15min periods)
3. Validate charging profile
4. Dispatch via OCPP SetChargingProfile with retry logic
5. Store dispatch results in database

If a charger is offline at dispatch time, the legacy WS handler enqueues
the profile to ``charging_command_queue`` (migration 013). On the next
BootNotification, ``OCPP16Session._on_boot`` triggers
``replay_queued_commands`` to flush pending rows for that charger within
the configured ``REPLAY_BACKOFF_SECONDS`` window.

## Optimization Readiness and Input Snapshots

Before the solver runs, `DepotController._capture_snapshot` evaluates
readiness against the assembled `DepotState` and writes a row to
`optimization_input_snapshots` (migrations 019, 020). The snapshot is
captured **once per logical run** — not per retry attempt — and the
`run_id` is back-filled after `optimization_runs` is persisted.

### Readiness verdicts

| Status | Meaning | Solver behaviour |
|---|---|---|
| `ready` | Every required input present | Run as-is |
| `degraded` | An input was substituted with an explicit assumption (e.g. building load forecast / static derate, defaulted telemetry) | Run with `optimization_runs.status='degraded'` |
| `not_ready` | A hard prerequisite is missing | `_ReadinessBlockedError` raised; no solve |

### Schedules-present check (VDV463 caveat)

`schedules_present` is evaluated against **raw DB schedules from the
`schedules` table BEFORE the VDV463 merge.** A depot that drives
optimization purely via VDV463 ProvideChargingRequests (no rows in the
`schedules` table) will be marked `not_ready` even though the merged
schedule list is non-empty. This is intentional: VDV463-only depots
should be flagged as a setup gap until proper route schedules are
imported. See `src/core/state/assembler.py` (presence captured before
`_merge_vdv463_into_schedules`) and `src/core/state/readiness.py`.

### Building-load source values

| Source | Triggered when | Effect |
|---|---|---|
| `meter` | `building_load` table has rows for the horizon | `ready` |
| `forecast_fallback` | Live source configured but data missing/stale | `degraded` (substitutes a deterministic business-hours pattern) |
| `static_assumption` | Depot configured `building_load_assumption_kw > 0` (HRX day-one) | `degraded`; MILP grid-balance adds a constant baseline equal to `building_load_assumption_kw` so `max_grid_kw` is still respected |
| `absent` | Source could not be resolved at all (e.g. n_steps=0) | `not_ready` |

## Error Handling and Resilience

### Retry Logic

- **Optimization**: 2 retries with exponential backoff (1s, 2s)
- **OCPP Dispatch**: Configurable retries (default: 3) with exponential backoff

### Circuit Breaker

Opens after `max_optimization_failures` consecutive failures:
- Prevents cascading failures
- Auto-resets after 30 minutes
- Logs all failures with context

### Cooldown Period

Prevents rapid re-optimization after triggers:
- Default: 5 minutes
- Configurable via `FAVONIUS_TRIGGER_COOLDOWN_MIN`
- Applies to all trigger types

### Graceful Shutdown

On application shutdown:
1. Stop accepting new optimization requests
2. Wait for current optimizations to complete (with timeout)
3. Stop trigger monitors gracefully
4. Cancel background tasks
5. Close database connections

## Monitoring and Metrics

### Prometheus Metrics

All metrics are prefixed with `favonius_`:

- `optimization_runs_total`: Counter by depot_id and trigger_reason
- `optimization_duration_seconds`: Histogram by depot_id
- `optimization_failures_total`: Counter by depot_id and failure_type
- `solver_used_total`: Counter by depot_id and solver ('gurobi' or 'highs')
- `solver_fallback_total`: Counter by depot_id and reason (license_failure, connection_error, etc.)
- `ocpp_dispatch_success_total`: Counter by depot_id
- `ocpp_dispatch_failures_total`: Counter by depot_id and error_type
- `control_loop_uptime_seconds`: Gauge by depot_id
- `controller_state`: Gauge by depot_id (1=running, 0=stopped)

### Solver Reliability

Per PRD Section 8.2, the system automatically falls back to HiGHS if Gurobi fails:
- Monitor Gurobi license status via health endpoint
- Track solver usage in metrics (`solver_used_total`)
- Log all fallback events with reason
- Health endpoint reports both Gurobi license status and HiGHS availability

### Health Checks

- **API Endpoint**: `GET /health` - Overall system health
- **Controller Health**: `GET /admin/controllers/{depot_id}/health` - Per-controller health
- **Controller List**: `GET /admin/controllers` - List all active controllers

## API Integration

### Endpoints

- `POST /optimize`: Trigger optimization (uses ControllerManager)
- `GET /admin/controllers`: List active controllers
- `GET /admin/controllers/{depot_id}/health`: Get controller health

### Controller Lifecycle

Controllers are automatically:
- Created on application startup for all depots
- Created on-demand via API if not exists
- Stopped gracefully on application shutdown

## Troubleshooting

### Controller Not Starting

**Symptoms**: No controllers in `/admin/controllers`

**Possible Causes**:
1. No depots in database
2. Database connection failure
3. Invalid depot configuration

**Solutions**:
1. Check database for depots: `SELECT * FROM sites` (Supabase)
2. Check application logs for errors
3. Verify depot configuration is valid

### Optimization Failures

**Symptoms**: High `optimization_failures_total` metric

**Possible Causes**:
1. State assembly failures (missing data)
2. Optimization timeout
3. Infeasible problem

**Solutions**:
1. Check vehicle telemetry data
2. Increase `FAVONIUS_OPTIMIZATION_TIMEOUT`
3. Review depot constraints (vehicle availability, energy requirements)

### Circuit Breaker Open

**Symptoms**: `circuit_breaker_open: true` in health check

**Solutions**:
1. Wait 30 minutes for auto-reset
2. Check logs for failure patterns
3. Verify depot configuration and data availability
4. Manually reset by restarting controller (restart application)

### OCPP Dispatch Failures

**Symptoms**: High `ocpp_dispatch_failures_total` metric

**Possible Causes**:
1. Charger not connected
2. Invalid charging profile
3. OCPP server unavailable

**Solutions**:
1. Check charger connection status
2. Verify vehicle-to-charger mapping in database
3. Check OCPP server logs
4. Review charging profile validation

### Control Loop Not Running

**Symptoms**: No hourly optimizations, `controller_state: 0`

**Possible Causes**:
1. Controller not started
2. Outside optimization hours
3. Controller stopped

**Solutions**:
1. Check `/admin/controllers` for controller existence
2. Verify optimization hours (default: 24/7)
3. Check application logs for errors
4. Restart application if needed

## Best Practices

1. **Monitor Metrics**: Set up alerts for high failure rates
2. **Log Analysis**: Review logs for patterns in failures
3. **Configuration**: Adjust timeouts and retries based on environment
4. **Health Checks**: Regularly check controller health endpoints
5. **Graceful Shutdown**: Always allow graceful shutdown to complete

## References

- [PRD_Depot_Agent.md](PRD_Depot_Agent.md): product direction (the agent uses the control loop as substrate)
- [ARCHITECTURE.md](ARCHITECTURE.md): system topology and data flow
- [API.md](API.md): REST + OCPP endpoint reference
- Code: `src/core/controller.py`
- Code: `src/core/controller_manager.py`
- Code: `src/core/controller_config.py`

