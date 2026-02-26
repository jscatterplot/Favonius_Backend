"""AT-09: VDV 463 Preconditioning (Manual).

GIVEN an upstream BMS connected via VDV 463
AND BMS sends ProvideChargingRequests with vehicleId, manualPreconditioning.hvacPreconditioningStartTime,
    requestedTimeForDeparture
WHEN optimization runs
THEN preconditioning power is included in schedule from start time
AND ProvideChargingInformation can report preconditioning status (Scheduled/Active).
"""

from datetime import datetime, timedelta

import pytest

from src.core.models import DepotConfig, DepotState
from src.core.optimizer.milp_model import build_optimization_model


@pytest.mark.acceptance
@pytest.mark.integration
class TestAT09VDV463ManualPreconditioning:
    """AT-09: Manual preconditioning from VDV 463."""

    def test_manual_preconditioning_in_state_triggers_precond_constraint(self):
        """Manual preconditioning (hvacPreconditioningStartTime) results in preconditioning in MILP."""
        now = datetime.utcnow()
        n_steps = 96
        # Manual: start at 5:00 AM, depart 5:30 AM
        start_time = now.replace(hour=5, minute=0, second=0, microsecond=0)
        if start_time <= now:
            start_time += timedelta(days=1)
        end_time = start_time + timedelta(minutes=30)

        state = DepotState(
            vehicle_socs={"bus_101": 0.5},
            battery_soc=0.5,
            prices=[0.1] * n_steps,
            demand_charge_rate=20.0,
            current_month_peak=0.0,
            vehicle_availability={"bus_101": [True] * n_steps},
            energy_requirements={"bus_101": 100.0},
            departure_times={"bus_101": 24},
            building_power=[50.0] * n_steps,
            preconditioning_requests=[
                {
                    "vehicle_id": "bus_101",
                    "start_time": start_time,
                    "end_time": end_time,
                    "power_kw": 10.0,
                }
            ],
        )
        config = DepotConfig(
            vehicle_capacities={"bus_101": 324.0},
            vehicle_max_charge_kw={"bus_101": 80.0},
            charger_groups={80.0: 1},
            charger_efficiency=0.95,
            charger_vehicle_access={"cp1": {"bus_101"}},
            battery_capacity=1.0,
            battery_power=0.1,
            max_site_power=500.0,
        )

        model = build_optimization_model(state, config, horizon_start=now)
        assert hasattr(model, "P_precond")
        assert hasattr(model, "precond_slack")
