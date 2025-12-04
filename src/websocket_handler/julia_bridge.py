"""Python-Julia bridge for MIP optimization solver."""

import asyncio
import json
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
import logging

from .monitoring import get_logger


class JuliaBridge:
    """Bridge between Python and Julia MIP solver."""
    
    def __init__(self, julia_path: str = "julia", solver_path: Optional[str] = None):
        """Initialize Julia bridge.
        
        Args:
            julia_path: Path to Julia executable
            solver_path: Path to Julia solver script (defaults to optimization/mip_solver.jl)
        """
        self.julia_path = julia_path
        self.solver_path = solver_path or str(Path(__file__).parent.parent / "optimization" / "mip_solver.jl")
        self.logger = get_logger(__name__)
        self._temp_dir = Path(tempfile.gettempdir()) / "julia_bridge"
        self._temp_dir.mkdir(exist_ok=True)
        
        # Verify Julia installation
        self._verify_julia_installation()
    
    def _verify_julia_installation(self) -> None:
        """Verify Julia installation and required packages."""
        try:
            result = subprocess.run(
                [self.julia_path, "--version"],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode != 0:
                raise RuntimeError(f"Julia not found at {self.julia_path}")
            
            self.logger.info(f"Julia version: {result.stdout.strip()}")
            
            # Check if required packages are installed
            self._check_julia_packages()
            
        except subprocess.TimeoutExpired:
            raise RuntimeError("Julia installation verification timed out")
        except FileNotFoundError:
            raise RuntimeError(f"Julia executable not found at {self.julia_path}")
    
    def _check_julia_packages(self) -> None:
        """Check if required Julia packages are installed."""
        required_packages = ["JuMP", "HiGHS", "JSON", "Dates"]
        
        for package in required_packages:
            try:
                result = subprocess.run(
                    [self.julia_path, "-e", f"using {package}"],
                    capture_output=True,
                    text=True,
                    timeout=30
                )
                if result.returncode != 0:
                    self.logger.warning(f"Julia package {package} not installed. Run: julia -e 'using Pkg; Pkg.add(\"{package}\")'")
            except subprocess.TimeoutExpired:
                self.logger.warning(f"Timeout checking Julia package {package}")
    
    async def solve_optimization(
        self,
        vehicles: List[Dict[str, Any]],
        optimization_params: Dict[str, Any],
        timeout_seconds: int = 30
    ) -> Dict[str, Any]:
        """Solve MIP optimization problem using Julia solver.
        
        Args:
            vehicles: List of vehicle data dictionaries
            optimization_params: Optimization parameters dictionary
            timeout_seconds: Maximum time to wait for solver
            
        Returns:
            Dictionary containing optimization results
        """
        try:
            # Prepare input data
            input_data = {
                "vehicles": vehicles,
                "params": optimization_params
            }
            
            # Create temporary files
            input_file = self._temp_dir / f"input_{int(time.time() * 1000)}.json"
            output_file = self._temp_dir / f"output_{int(time.time() * 1000)}.json"
            
            # Write input data
            with open(input_file, 'w') as f:
                json.dump(input_data, f, indent=2, default=str)
            
            # Run Julia solver
            start_time = time.time()
            
            process = await asyncio.create_subprocess_exec(
                self.julia_path,
                self.solver_path,
                str(input_file),
                str(output_file),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout_seconds
                )
                
                solve_time = (time.time() - start_time) * 1000  # milliseconds
                
                if process.returncode != 0:
                    error_msg = stderr.decode() if stderr else "Unknown error"
                    self.logger.error(f"Julia solver failed: {error_msg}")
                    return {
                        "status": "ERROR",
                        "error": error_msg,
                        "solve_time_ms": solve_time
                    }
                
                # Read output data
                if output_file.exists():
                    with open(output_file, 'r') as f:
                        result = json.load(f)
                    
                    # Add solve time if not present
                    if "solve_time_ms" not in result:
                        result["solve_time_ms"] = solve_time
                    
                    self.logger.info(f"Optimization completed: {result.get('status', 'UNKNOWN')}")
                    return result
                else:
                    return {
                        "status": "ERROR",
                        "error": "Output file not created",
                        "solve_time_ms": solve_time
                    }
                    
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return {
                    "status": "TIMEOUT",
                    "error": f"Solver timed out after {timeout_seconds} seconds",
                    "solve_time_ms": timeout_seconds * 1000
                }
                
        except Exception as e:
            self.logger.error(f"Error in Julia bridge: {e}")
            return {
                "status": "ERROR",
                "error": str(e),
                "solve_time_ms": 0.0
            }
        finally:
            # Clean up temporary files
            try:
                if input_file.exists():
                    input_file.unlink()
                if output_file.exists():
                    output_file.unlink()
            except Exception as e:
                self.logger.warning(f"Failed to clean up temporary files: {e}")
    
    def prepare_vehicle_data(
        self,
        vehicle_id: str,
        battery_capacity_kwh: float,
        max_charge_rate_kw: float,
        max_discharge_rate_kw: float,
        initial_soc_kwh: float,
        min_soc_kwh: float,
        charge_efficiency: float = 0.95,
        discharge_efficiency: float = 0.90,
        departure_time: Optional[datetime] = None,
        required_soc_kwh: Optional[float] = None
    ) -> Dict[str, Any]:
        """Prepare vehicle data for Julia solver.
        
        Args:
            vehicle_id: Unique vehicle identifier
            battery_capacity_kwh: Battery capacity in kWh
            max_charge_rate_kw: Maximum charging power in kW
            max_discharge_rate_kw: Maximum discharging power in kW
            initial_soc_kwh: Initial state of charge in kWh
            min_soc_kwh: Minimum allowed state of charge in kWh
            charge_efficiency: Charging efficiency (0-1)
            discharge_efficiency: Discharging efficiency (0-1)
            departure_time: When vehicle needs to depart
            required_soc_kwh: Required SOC at departure time
            
        Returns:
            Dictionary with vehicle data for Julia solver
        """
        return {
            "vehicle_id": vehicle_id,
            "battery_capacity_kwh": battery_capacity_kwh,
            "max_charge_rate_kw": max_charge_rate_kw,
            "max_discharge_rate_kw": max_discharge_rate_kw,
            "initial_soc_kwh": initial_soc_kwh,
            "min_soc_kwh": min_soc_kwh,
            "charge_efficiency": charge_efficiency,
            "discharge_efficiency": discharge_efficiency,
            "departure_time": departure_time.isoformat() if departure_time else None,
            "required_soc_kwh": required_soc_kwh or battery_capacity_kwh * 0.8
        }
    
    def prepare_optimization_params(
        self,
        horizon_hours: int = 24,
        timestep_minutes: int = 15,
        facility_capacity_kw: float = 1000.0,
        demand_charge_rate_per_kw: float = 15.0,
        electricity_prices: List[float] = None,
        forecast_demand: List[float] = None,
        cost_weight: float = 1.0,
        peak_weight: float = 0.1
    ) -> Dict[str, Any]:
        """Prepare optimization parameters for Julia solver.
        
        Args:
            horizon_hours: Optimization horizon in hours
            timestep_minutes: Time step size in minutes
            facility_capacity_kw: Maximum facility charging capacity
            demand_charge_rate_per_kw: Demand charge rate per kW
            electricity_prices: Electricity prices for each timestep
            forecast_demand: Forecasted demand for each timestep
            cost_weight: Weight for cost objective
            peak_weight: Weight for peak demand objective
            
        Returns:
            Dictionary with optimization parameters
        """
        n_timesteps = int(horizon_hours * 60 / timestep_minutes)
        
        if electricity_prices is None:
            # Default flat price
            electricity_prices = [0.12] * n_timesteps
        
        if forecast_demand is None:
            # Default flat demand
            forecast_demand = [100.0] * n_timesteps
        
        return {
            "horizon_hours": horizon_hours,
            "timestep_minutes": timestep_minutes,
            "facility_capacity_kw": facility_capacity_kw,
            "demand_charge_rate_per_kw": demand_charge_rate_per_kw,
            "electricity_prices": electricity_prices,
            "forecast_demand": forecast_demand,
            "cost_weight": cost_weight,
            "peak_weight": peak_weight
        }
    
    async def test_solver(self) -> bool:
        """Test Julia solver with a simple problem.
        
        Returns:
            True if solver works correctly, False otherwise
        """
        try:
            # Create a simple test case
            vehicles = [
                self.prepare_vehicle_data(
                    vehicle_id="test_vehicle_1",
                    battery_capacity_kwh=75.0,
                    max_charge_rate_kw=22.0,
                    max_discharge_rate_kw=10.0,
                    initial_soc_kwh=30.0,
                    min_soc_kwh=15.0,
                    departure_time=datetime.now(timezone.utc).replace(hour=8, minute=0, second=0, microsecond=0),
                    required_soc_kwh=60.0
                )
            ]
            
            params = self.prepare_optimization_params(
                horizon_hours=4,
                timestep_minutes=15,
                facility_capacity_kw=50.0,
                electricity_prices=[0.10, 0.12, 0.15, 0.12, 0.10, 0.08, 0.10, 0.12, 0.15, 0.18, 0.20, 0.18, 0.15, 0.12, 0.10, 0.08]
            )
            
            result = await self.solve_optimization(vehicles, params, timeout_seconds=10)
            
            if result["status"] == "OPTIMAL":
                self.logger.info("Julia solver test passed")
                return True
            else:
                self.logger.error(f"Julia solver test failed: {result.get('error', 'Unknown error')}")
                return False
                
        except Exception as e:
            self.logger.error(f"Julia solver test error: {e}")
            return False
    
    def cleanup(self) -> None:
        """Clean up temporary files."""
        try:
            if self._temp_dir.exists():
                for file in self._temp_dir.glob("*.json"):
                    try:
                        file.unlink()
                    except Exception:
                        pass
        except Exception as e:
            self.logger.warning(f"Failed to cleanup temporary files: {e}")


# Convenience function for direct usage
async def solve_ev_optimization(
    vehicles: List[Dict[str, Any]],
    optimization_params: Dict[str, Any],
    julia_path: str = "julia"
) -> Dict[str, Any]:
    """Convenience function to solve EV optimization problem.
    
    Args:
        vehicles: List of vehicle data dictionaries
        optimization_params: Optimization parameters dictionary
        julia_path: Path to Julia executable
        
    Returns:
        Dictionary containing optimization results
    """
    bridge = JuliaBridge(julia_path)
    try:
        return await bridge.solve_optimization(vehicles, optimization_params)
    finally:
        bridge.cleanup()
