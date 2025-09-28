# Julia Optimization Engine for V2G Dispatch

## System Architecture
The Julia optimization engine performs real-time dispatch optimization for EV fleets, balancing grid needs, electricity prices, and fleet constraints within 30-second decision cycles.

## Core Optimization Model

### Problem Formulation
The V2G dispatch problem is formulated as a mixed-integer linear program (MILP) that minimizes total operational costs while respecting fleet and grid constraints.

### Objective Function
```julia
# Minimize: Energy costs - V2G revenue + Demand charges + Battery degradation
minimize(
    sum(price[t] * charge_power[v,t] for v in vehicles, t in time_periods) -
    sum(price[t] * discharge_power[v,t] for v in vehicles, t in time_periods) +
    demand_charge_rate * peak_demand +
    degradation_cost * sum(discharge_power[v,t] for v in vehicles, t in time_periods)
)
```

### Decision Variables
```julia
# Continuous variables
@variable(model, 0 <= charge_power[v,t] <= charge_limit[v])
@variable(model, 0 <= discharge_power[v,t] <= discharge_limit[v])
@variable(model, soc_min[v] <= soc[v,t] <= soc_max[v])
@variable(model, 0 <= peak_demand)

# Binary variables
@variable(model, is_charging[v,t], Bin)
@variable(model, is_discharging[v,t], Bin)
@variable(model, is_connected[v,t], Bin)
```

### Constraints

#### Power Balance
```julia
@constraint(model, power_balance[t in time_periods],
    sum(charge_power[v,t] for v in vehicles) -
    sum(discharge_power[v,t] for v in vehicles) +
    base_load[t] <= grid_capacity[t]
)
```

#### State of Charge Dynamics
```julia
@constraint(model, soc_dynamics[v in vehicles, t in time_periods[2:end]],
    soc[v,t] == soc[v,t-1] + 
    (charge_power[v,t-1] * charge_efficiency - 
     discharge_power[v,t-1] / discharge_efficiency) * 
    time_step / battery_capacity[v]
)
```

#### Mutual Exclusion
```julia
@constraint(model, mutex[v in vehicles, t in time_periods],
    is_charging[v,t] + is_discharging[v,t] <= is_connected[v,t]
)
```

#### Departure Constraints
```julia
@constraint(model, departure_soc[v in vehicles],
    soc[v, departure_time[v]] >= required_soc[v]
)
```

## Implementation Structure

### Main Optimization Module
```julia
module V2GOptimization

using JuMP
using Gurobi  # or HiGHS for open-source
using DataFrames
using Dates
using Redis
using HTTP
using JSON3
using TimerOutputs

export optimize_fleet_dispatch, FleetState, OptimizationResult

# Configuration structure
struct OptimizationConfig
    time_horizon::Int  # minutes
    time_step::Int     # seconds
    mip_gap::Float64
    time_limit::Float64
    degradation_cost::Float64
    demand_charge_rate::Float64
end

# Fleet state representation
struct FleetState
    vehicles::Vector{VehicleState}
    grid_capacity::Vector{Float64}
    base_load::Vector{Float64}
    prices::Vector{Float64}
    timestamp::DateTime
end

struct VehicleState
    id::String
    soc::Float64
    battery_capacity::Float64
    charge_limit::Float64
    discharge_limit::Float64
    departure_time::DateTime
    required_soc::Float64
    is_connected::Bool
end

# Main optimization function
function optimize_fleet_dispatch(
    fleet_state::FleetState,
    config::OptimizationConfig
)::OptimizationResult
    
    @timeit "model_build" model = build_optimization_model(fleet_state, config)
    @timeit "solve" optimize!(model)
    @timeit "extract" result = extract_solution(model, fleet_state)
    
    return result
end
```

### Model Building
```julia
function build_optimization_model(fleet::FleetState, config::OptimizationConfig)
    model = Model(Gurobi.Optimizer)
    
    # Set solver parameters
    set_optimizer_attribute(model, "MIPGap", config.mip_gap)
    set_optimizer_attribute(model, "TimeLimit", config.time_limit)
    set_optimizer_attribute(model, "Threads", 8)
    
    # Time sets
    T = 1:div(config.time_horizon * 60, config.time_step)
    V = 1:length(fleet.vehicles)
    
    # Variables
    @variable(model, 0 <= charge[v in V, t in T] <= fleet.vehicles[v].charge_limit)
    @variable(model, 0 <= discharge[v in V, t in T] <= fleet.vehicles[v].discharge_limit)
    @variable(model, fleet.vehicles[v].soc * 0.2 <= soc[v in V, t in T] <= fleet.vehicles[v].soc)
    @variable(model, peak_demand >= 0)
    
    # Binary variables for operation mode
    @variable(model, is_charging[v in V, t in T], Bin)
    @variable(model, is_discharging[v in V, t in T], Bin)
    
    # Objective function
    @objective(model, Min,
        sum(fleet.prices[t] * charge[v,t] * config.time_step / 3600 for v in V, t in T) -
        sum(fleet.prices[t] * discharge[v,t] * config.time_step / 3600 for v in V, t in T) +
        config.demand_charge_rate * peak_demand +
        config.degradation_cost * sum(discharge[v,t] * config.time_step / 3600 for v in V, t in T)
    )
    
    # Add constraints
    add_power_constraints!(model, fleet, charge, discharge, peak_demand)
    add_soc_constraints!(model, fleet, charge, discharge, soc, config)
    add_departure_constraints!(model, fleet, soc)
    add_grid_constraints!(model, fleet, charge, discharge)
    
    return model
end
```

### Real-Time Data Interface

#### Redis Connection
```julia
struct RedisConnection
    client::Redis.RedisClient
    pipeline::Redis.Pipeline
end

function connect_redis(host::String, port::Int)
    client = Redis.RedisClient(host=host, port=port)
    return RedisConnection(client, Redis.Pipeline(client))
end

function fetch_fleet_state(redis::RedisConnection, operator_id::String)::FleetState
    # Fetch all vehicle states
    vehicle_keys = Redis.keys(redis.client, "charger:state:*")
    vehicles = Vector{VehicleState}()
    
    for key in vehicle_keys
        data = Redis.hgetall(redis.client, key)
        push!(vehicles, parse_vehicle_state(data))
    end
    
    # Fetch grid data
    grid_capacity = fetch_grid_capacity(redis)
    base_load = fetch_base_load(redis)
    prices = fetch_electricity_prices(redis)
    
    return FleetState(vehicles, grid_capacity, base_load, prices, now())
end
```

#### CAISO Price Integration
```julia
function fetch_electricity_prices(node_id::String)::Vector{Float64}
    # Query CAISO OASIS API
    base_url = "https://oasis.caiso.com/oasisapi/SingleZip"
    params = Dict(
        "queryname" => "PRC_RTPD_LMP",
        "market_run_id" => "RTPD",
        "node" => node_id,
        "startdatetime" => Dates.format(now(), "yyyymmddTHH:MM"),
        "enddatetime" => Dates.format(now() + Hour(1), "yyyymmddTHH:MM"),
        "resultformat" => "6"
    )
    
    response = HTTP.get(base_url, query=params)
    prices = parse_caiso_response(response.body)
    
    return prices
end
```

### Advanced Optimization Features

#### Rolling Horizon Implementation
```julia
function rolling_horizon_optimization(
    initial_state::FleetState,
    config::OptimizationConfig,
    update_interval::Int  # seconds
)
    current_state = initial_state
    
    while true
        # Solve for current horizon
        result = optimize_fleet_dispatch(current_state, config)
        
        # Apply only first time step
        apply_first_decision(result)
        
        # Wait and update state
        sleep(update_interval)
        current_state = fetch_updated_state()
        
        # Warm start next iteration
        warm_start_model(result, current_state)
    end
end
```

#### Stochastic Optimization
```julia
function stochastic_dispatch(
    fleet::FleetState,
    scenarios::Vector{PriceScenario},
    config::OptimizationConfig
)
    model = Model(Gurobi.Optimizer)
    
    # First-stage variables (immediate decisions)
    @variable(model, charge_now[v in vehicles] >= 0)
    @variable(model, discharge_now[v in vehicles] >= 0)
    
    # Second-stage variables (recourse decisions per scenario)
    S = length(scenarios)
    @variable(model, charge_future[v in vehicles, t in 2:T, s in 1:S] >= 0)
    @variable(model, discharge_future[v in vehicles, t in 2:T, s in 1:S] >= 0)
    
    # Expected value objective
    @objective(model, Min,
        sum(scenarios[s].probability * scenario_cost[s] for s in 1:S)
    )
    
    # Non-anticipativity constraints
    @constraint(model, first_stage_decision[v in vehicles],
        charge_now[v] == charge_future[v, 1, 1]
    )
    
    return model
end
```

#### Frequency Regulation Co-optimization
```julia
function optimize_with_frequency_regulation(
    fleet::FleetState,
    regulation_signal::Vector{Float64},
    config::OptimizationConfig
)
    model = build_optimization_model(fleet, config)
    
    # Add regulation variables
    @variable(model, reg_up_capacity[t in T] >= 0)
    @variable(model, reg_down_capacity[t in T] >= 0)
    
    # Regulation constraints
    @constraint(model, reg_up_constraint[t in T],
        sum(charge_limit[v] - charge[v,t] for v in V) >= reg_up_capacity[t]
    )
    
    @constraint(model, reg_down_constraint[t in T],
        sum(discharge_limit[v] - discharge[v,t] for v in V) >= reg_down_capacity[t]
    )
    
    # Add regulation revenue to objective
    regulation_price = 0.05  # $/kW
    @objective(model, Min,
        objective_function(model) - 
        regulation_price * sum(reg_up_capacity[t] + reg_down_capacity[t] for t in T)
    )
    
    return model
end
```

### Performance Optimization

#### Parallel Processing
```julia
using Distributed
using SharedArrays

# Add worker processes
addprocs(4)

@everywhere using V2GOptimization

function parallel_scenario_optimization(scenarios::Vector{Scenario})
    # Distribute scenarios across workers
    results = pmap(optimize_scenario, scenarios)
    
    # Aggregate results
    return aggregate_scenario_results(results)
end
```

#### Model Warm Starting
```julia
function warm_start_from_previous(
    model::Model,
    previous_solution::OptimizationResult
)
    # Set variable start values
    for (v, t) in keys(previous_solution.charge_schedule)
        set_start_value(model[:charge][v,t], previous_solution.charge_schedule[v,t])
    end
    
    for (v, t) in keys(previous_solution.discharge_schedule)
        set_start_value(model[:discharge][v,t], previous_solution.discharge_schedule[v,t])
    end
    
    # Provide initial dual values if using interior point
    if solver_name(model) == "Gurobi"
        set_optimizer_attribute(model, "Method", 2)  # Barrier method
    end
end
```

### Solution Output Format

#### Result Structure
```julia
struct OptimizationResult
    decision_id::UUID
    timestamp::DateTime
    objective_value::Float64
    solve_time::Float64
    charge_schedule::Dict{Tuple{Int,Int}, Float64}
    discharge_schedule::Dict{Tuple{Int,Int}, Float64}
    soc_trajectory::Dict{Tuple{Int,Int}, Float64}
    peak_demand::Float64
    computation_stats::Dict{String, Any}
end

function publish_to_redis(redis::RedisConnection, result::OptimizationResult)
    # Serialize to JSON
    result_json = JSON3.write(result)
    
    # Store in Redis
    Redis.hset(redis.client, "optimization:decision:latest", 
              "data", result_json)
    
    # Publish to subscribers
    Redis.publish(redis.client, "notify:optimization:complete", result_json)
    
    # Store in history
    Redis.lpush(redis.client, "optimization:decisions", result_json)
    Redis.ltrim(redis.client, "optimization:decisions", 0, 99)
end
```

## Deployment Configuration

### Package Dependencies
```toml
[deps]
JuMP = "4076af6c-e467-56ae-b986-b466b2749572"
Gurobi = "2e9cd046-0924-5485-92f1-d5272153d98b"
HiGHS = "87dc4568-4c63-4d18-b0c0-bb2238e4078b"
PowerModels = "c36e90e8-916a-50a6-bd94-e7b1b8b7c6e5"
DataFrames = "a93c6f00-e57d-5684-b7b6-d8193f3e46c0"
Redis = "0be275d4-229f-54bb-9561-ceee5e3e8dae"
HTTP = "cd3eb016-35fb-5094-929b-558a96fad6f3"
JSON3 = "0f8b85d8-7281-11e9-16c2-39a750bddbf1"
TimerOutputs = "a759f4b9-e2f1-59dc-863e-4aeb61b1ea8f"
Distributed = "8ba89e20-285c-5b6f-9357-94700520ee1b"
```

### Environment Variables
```bash
JULIA_NUM_THREADS=8
GUROBI_LICENSE_FILE=/opt/gurobi/license.lic
REDIS_HOST=redis-master-1.v2g.internal
REDIS_PORT=6379
CAISO_NODE_ID=TH_SP15_GEN-APND
OPTIMIZATION_INTERVAL=30
MIP_GAP=0.001
TIME_LIMIT=25
```

### Container Configuration
```dockerfile
FROM julia:1.9

# Install Gurobi
RUN apt-get update && apt-get install -y wget
RUN wget https://packages.gurobi.com/9.5/gurobi9.5.2_linux64.tar.gz
RUN tar -xzf gurobi9.5.2_linux64.tar.gz -C /opt/

# Set Gurobi environment
ENV GUROBI_HOME=/opt/gurobi952/linux64
ENV PATH="${PATH}:${GUROBI_HOME}/bin"
ENV LD_LIBRARY_PATH="${LD_LIBRARY_PATH}:${GUROBI_HOME}/lib"

# Install Julia packages
COPY Project.toml Manifest.toml ./
RUN julia -e 'using Pkg; Pkg.activate("."); Pkg.instantiate()'

# Copy application
COPY src/ ./src/
COPY scripts/ ./scripts/

CMD ["julia", "--project=.", "src/main.jl"]
```

## Monitoring and Diagnostics

### Performance Metrics
- Optimization solve time per iteration
- MIP gap achieved
- Number of iterations to convergence
- Constraint violations
- Solution quality metrics

### Logging Configuration
```julia
using Logging
using LoggingExtras

logger = ConsoleLogger(
    stderr,
    Logging.Info;
    meta_formatter=default_metaformatter,
    show_limited=true,
    right_justify=0
)

global_logger(TeeLogger(
    logger,
    FileLogger("optimization.log")
))
```

### Health Checks
```julia
function health_check()
    # Test solver availability
    model = Model(Gurobi.Optimizer)
    @variable(model, x >= 0)
    @objective(model, Min, x)
    optimize!(model)
    
    if termination_status(model) != MOI.OPTIMAL
        error("Solver health check failed")
    end
    
    # Test Redis connectivity
    redis_ping()
    
    # Test CAISO API
    test_price_fetch()
    
    return true
end
```