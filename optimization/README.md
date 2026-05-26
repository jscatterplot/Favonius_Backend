# Optimization Implementation

## Primary Implementation

**Per `docs/PRD_Depot_Agent.md` and CLAUDE.md (Solver Configuration)**, the primary optimization implementation is:

- **Language**: Python 3.12
- **Modeling Library**: Pyomo
- **Primary Solver**: Gurobi (commercial, high performance)
- **Fallback Solver**: HiGHS (open-source, automatic fallback on Gurobi failure)

**Location**: `src/core/optimizer/`

**Reference**: `docs/PRD_Depot_Agent.md` / CLAUDE.md (Optimization Engine Specifications)

## Julia Reference Implementation

The `mip_solver.jl` file in this directory is a **reference implementation** using:

- **Language**: Julia
- **Modeling Library**: JuMP
- **Solver**: HiGHS

**Status**: This is a reference/prototype implementation and is **not** the primary production implementation. It may be useful for:
- Algorithm validation
- Performance benchmarking
- Research/development purposes

**Note**: The production system uses Python + Pyomo + Gurobi (with HiGHS fallback) as specified in `docs/PRD_Depot_Agent.md` / CLAUDE.md (Solver Configuration).

## Solver Configuration

### Primary: Gurobi
- Time limit: 60 seconds
- MIP optimality gap: 1% (0.01)
- Threads: 4 (adjustable)
- Presolve: Aggressive (2)
- NumericFocus: 3 (highest accuracy)
- WarmStart: Enabled

### Fallback: HiGHS
- Time limit: 60 seconds
- MIP relative gap: 1% (0.01)
- Threads: 4 (adjustable)
- Presolve: Enabled

**Reference**: `docs/PRD_Depot_Agent.md` / CLAUDE.md (Solver Configuration)

## Solver Reliability

Per PRD Section 8.2, the system automatically falls back to HiGHS if Gurobi fails (license failure, connection issues, or solver errors). This ensures graceful degradation rather than complete system failure.

The `solver_used` field in `OptimizationResult` tracks which solver was used for monitoring and analysis.

**Reference**: `docs/PRD_Depot_Agent.md` / CLAUDE.md (Solver Selection Logic)

