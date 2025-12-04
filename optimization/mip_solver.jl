using JuMP
using HiGHS
using JSON
using Dates

"""
Julia/JuMP-based MIP solver for EV fleet charging optimization.
Solves the optimal charging schedule for a fleet of electric vehicles
over a 24-hour horizon with cost minimization and peak demand smoothing.
"""

struct VehicleData
    vehicle_id::String
    battery_capacity_kwh::Float64
    max_charge_rate_kw::Float64
    max_discharge_rate_kw::Float64
    initial_soc_kwh::Float64
    min_soc_kwh::Float64
    charge_efficiency::Float64
    discharge_efficiency::Float64
    departure_time::Union{DateTime, Nothing}
    required_soc_kwh::Float64
end

struct OptimizationParams
    horizon_hours::Int
    timestep_minutes::Int
    facility_capacity_kw::Float64
    demand_charge_rate_per_kw::Float64
    electricity_prices::Vector{Float64}
    forecast_demand::Vector{Float64}
    cost_weight::Float64
    peak_weight::Float64
    demand_weight::Float64
end

struct OptimizationResult
    status::String
    objective_value::Float64
    solve_time_ms::Float64
    charging_schedule::Matrix{Float64}  # vehicles × timesteps
    discharging_schedule::Matrix{Float64}
    soc_trajectory::Matrix{Float64}
    peak_demand_kw::Float64
    total_cost::Float64
    total_energy_charged::Float64
    total_energy_discharged::Float64
end

function solve_charging_optimization(vehicles::Vector{VehicleData}, params::OptimizationParams)
    """
    Solve the MIP optimization problem for EV fleet charging.
    
    Returns OptimizationResult with the optimal charging schedule.
    """
    
    n_vehicles = length(vehicles)
    n_timesteps = Int(params.horizon_hours * 60 / params.timestep_minutes)
    timestep_hours = params.timestep_minutes / 60.0
    
    # Create JuMP model
    model = Model(HiGHS.Optimizer)
    set_silent(model)
    
    # Decision variables
    @variable(model, p_charge[1:n_vehicles, 1:n_timesteps] >= 0)  # charging power (kW)
    @variable(model, p_discharge[1:n_vehicles, 1:n_timesteps] >= 0)  # discharging power (kW)
    @variable(model, is_charging[1:n_vehicles, 1:n_timesteps], Bin)  # binary charging state
    @variable(model, soc[1:n_vehicles, 1:n_timesteps])  # state of charge (kWh)
    @variable(model, peak_demand >= 0)  # peak demand for entire horizon (kW)
    
    # Multi-objective function: minimize weighted sum of cost, peak demand, and demand forecast deviation
    @objective(model, Min,
        params.cost_weight * sum(
            params.electricity_prices[t] * (p_charge[v,t] - p_discharge[v,t]) * timestep_hours
            for v in 1:n_vehicles, t in 1:n_timesteps
        ) +
        params.peak_weight * params.demand_charge_rate_per_kw * peak_demand +
        params.demand_weight * sum(
            abs(sum(p_charge[v,t] - p_discharge[v,t] for v in 1:n_vehicles) - 
                (t <= length(params.forecast_demand) ? params.forecast_demand[t] : 0.0))
            for t in 1:n_timesteps
        )
    )
    
    # Constraints
    
    # Initial SOC constraints
    for v in 1:n_vehicles
        @constraint(model, soc[v,1] == vehicles[v].initial_soc_kwh)
    end
    
    # SOC evolution constraints
    for v in 1:n_vehicles, t in 2:n_timesteps
        @constraint(model, 
            soc[v,t] == soc[v,t-1] + 
            (p_charge[v,t-1] * vehicles[v].charge_efficiency - 
             p_discharge[v,t-1] / vehicles[v].discharge_efficiency) * timestep_hours
        )
    end
    
    # SOC bounds
    for v in 1:n_vehicles, t in 1:n_timesteps
        @constraint(model, vehicles[v].min_soc_kwh <= soc[v,t] <= vehicles[v].battery_capacity_kwh)
    end
    
    # Departure readiness constraints
    for v in 1:n_vehicles
        if vehicles[v].departure_time !== nothing
            departure_timestep = calculate_timestep(vehicles[v].departure_time, params)
            if departure_timestep <= n_timesteps
                @constraint(model, soc[v, departure_timestep] >= vehicles[v].required_soc_kwh)
            end
        end
    end
    
    # Charging power limits
    for v in 1:n_vehicles, t in 1:n_timesteps
        @constraint(model, p_charge[v,t] <= vehicles[v].max_charge_rate_kw * is_charging[v,t])
    end
    
    # Discharging power limits (V2G)
    for v in 1:n_vehicles, t in 1:n_timesteps
        @constraint(model, p_discharge[v,t] <= vehicles[v].max_discharge_rate_kw * (1 - is_charging[v,t]))
    end
    
    # Peak demand tracking
    for t in 1:n_timesteps
        @constraint(model, 
            sum(p_charge[v,t] - p_discharge[v,t] for v in 1:n_vehicles) <= peak_demand
        )
    end
    
    # Facility capacity constraints
    for t in 1:n_timesteps
        @constraint(model, sum(p_charge[v,t] for v in 1:n_vehicles) <= params.facility_capacity_kw)
    end
    
    # Solve the model
    start_time = time()
    optimize!(model)
    solve_time = (time() - start_time) * 1000  # convert to milliseconds
    
    # Extract results
    status = termination_status(model)
    if status == MOI.OPTIMAL
        charging_schedule = value.(p_charge)
        discharging_schedule = value.(p_discharge)
        soc_trajectory = value.(soc)
        peak_demand_kw = value(peak_demand)
        objective_value = objective_value(model)
        
        # Calculate additional metrics
        total_cost = sum(
            params.electricity_prices[t] * (charging_schedule[v,t] - discharging_schedule[v,t]) * timestep_hours
            for v in 1:n_vehicles, t in 1:n_timesteps
        ) + params.demand_charge_rate_per_kw * peak_demand_kw
        
        total_energy_charged = sum(charging_schedule) * timestep_hours
        total_energy_discharged = sum(discharging_schedule) * timestep_hours
        
        return OptimizationResult(
            "OPTIMAL",
            objective_value,
            solve_time,
            charging_schedule,
            discharging_schedule,
            soc_trajectory,
            peak_demand_kw,
            total_cost,
            total_energy_charged,
            total_energy_discharged
        )
    else
        return OptimizationResult(
            string(status),
            0.0,
            solve_time,
            zeros(n_vehicles, n_timesteps),
            zeros(n_vehicles, n_timesteps),
            zeros(n_vehicles, n_timesteps),
            0.0,
            0.0,
            0.0,
            0.0
        )
    end
end

function calculate_timestep(departure_time::DateTime, params::OptimizationParams)
    """Calculate which timestep corresponds to the departure time."""
    # Assuming optimization starts at current time
    current_time = now()
    time_diff = departure_time - current_time
    timestep = Int(floor(time_diff / Minute(params.timestep_minutes))) + 1
    return max(1, timestep)
end

function main()
    """Main function for command-line usage."""
    if length(ARGS) != 2
        println("Usage: julia mip_solver.jl input.json output.json")
        exit(1)
    end
    
    input_file = ARGS[1]
    output_file = ARGS[2]
    
    try
        # Read input data
        input_data = JSON.parsefile(input_file)
        
        # Parse vehicles
        vehicles = Vector{VehicleData}()
        for v_data in input_data["vehicles"]
            push!(vehicles, VehicleData(
                v_data["vehicle_id"],
                v_data["battery_capacity_kwh"],
                v_data["max_charge_rate_kw"],
                v_data["max_discharge_rate_kw"],
                v_data["initial_soc_kwh"],
                v_data["min_soc_kwh"],
                v_data["charge_efficiency"],
                v_data["discharge_efficiency"],
                v_data["departure_time"] === nothing ? nothing : DateTime(v_data["departure_time"]),
                v_data["required_soc_kwh"]
            ))
        end
        
        # Parse optimization parameters
        params = OptimizationParams(
            input_data["params"]["horizon_hours"],
            input_data["params"]["timestep_minutes"],
            input_data["params"]["facility_capacity_kw"],
            input_data["params"]["demand_charge_rate_per_kw"],
            input_data["params"]["electricity_prices"],
            input_data["params"]["forecast_demand"],
            input_data["params"]["cost_weight"],
            input_data["params"]["peak_weight"],
            get(input_data["params"], "demand_weight", 0.05)  # Default demand weight
        )
        
        # Solve optimization
        result = solve_charging_optimization(vehicles, params)
        
        # Prepare output data
        output_data = Dict(
            "status" => result.status,
            "objective_value" => result.objective_value,
            "solve_time_ms" => result.solve_time_ms,
            "charging_schedule" => result.charging_schedule,
            "discharging_schedule" => result.discharging_schedule,
            "soc_trajectory" => result.soc_trajectory,
            "peak_demand_kw" => result.peak_demand_kw,
            "total_cost" => result.total_cost,
            "total_energy_charged" => result.total_energy_charged,
            "total_energy_discharged" => result.total_energy_discharged
        )
        
        # Write output
        open(output_file, "w") do f
            JSON.print(f, output_data, 4)
        end
        
        println("Optimization completed successfully")
        println("Status: $(result.status)")
        println("Solve time: $(round(result.solve_time_ms, digits=2)) ms")
        println("Total cost: \$$(round(result.total_cost, digits=2))")
        println("Peak demand: $(round(result.peak_demand_kw, digits=2)) kW")
        
    catch e
        error_data = Dict(
            "status" => "ERROR",
            "error" => string(e),
            "solve_time_ms" => 0.0
        )
        
        open(output_file, "w") do f
            JSON.print(f, error_data, 4)
        end
        
        println("Optimization failed: $e")
        exit(1)
    end
end

# Multi-objective optimization with Pareto frontier analysis
function solve_multi_objective_optimization(vehicles::Vector{VehicleData}, params::OptimizationParams; num_solutions::Int = 5)
    """
    Solve multi-objective optimization with Pareto frontier analysis.
    Returns multiple solutions with different weight combinations.
    """
    
    # Define weight combinations for Pareto frontier
    weight_combinations = [
        (1.0, 0.0, 0.0),  # Cost only
        (0.0, 1.0, 0.0),  # Peak only
        (0.0, 0.0, 1.0),  # Demand forecast only
        (0.5, 0.3, 0.2),  # Balanced
        (0.3, 0.5, 0.2)   # Peak-focused
    ]
    
    pareto_solutions = Vector{Dict}()
    
    for (i, (cost_w, peak_w, demand_w)) in enumerate(weight_combinations[1:min(num_solutions, length(weight_combinations))])
        # Create modified params with new weights
        modified_params = OptimizationParams(
            params.horizon_hours,
            params.timestep_minutes,
            params.facility_capacity_kw,
            params.demand_charge_rate_per_kw,
            params.electricity_prices,
            params.forecast_demand,
            cost_w,
            peak_w,
            demand_w
        )
        
        result = solve_charging_optimization(vehicles, modified_params)
        
        if result.status == "OPTIMAL"
            push!(pareto_solutions, Dict(
                "solution_id" => i,
                "weights" => Dict("cost" => cost_w, "peak" => peak_w, "demand" => demand_w),
                "objective_value" => result.objective_value,
                "total_cost" => result.total_cost,
                "peak_demand_kw" => result.peak_demand_kw,
                "charging_schedule" => result.charging_schedule,
                "discharging_schedule" => result.discharging_schedule,
                "soc_trajectory" => result.soc_trajectory,
                "solve_time_ms" => result.solve_time_ms
            ))
        end
    end
    
    return Dict(
        "status" => "OPTIMAL",
        "pareto_solutions" => pareto_solutions,
        "num_solutions" => length(pareto_solutions)
    )
end

# Run main function if called directly
if abspath(PROGRAM_FILE) == @__FILE__
    main()
end
