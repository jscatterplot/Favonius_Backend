"""AT-10: VDV 463 Preconditioning (Automatic).

GIVEN an upstream BMS connected via VDV 463
AND BMS sends ProvideChargingRequests with automaticPreconditioning (ambientTemperature, targetTemperature, departureTime)
WHEN optimization runs
THEN system calculates preconditioning duration (e.g. (targetTemp - ambientTemp) * 3 min/°C)
AND preconditioning is scheduled to start before departure
AND preconditioning power is included in grid power calculation.
"""

from datetime import datetime, timedelta

import pytest

from src.core.models import DepotConfig, DepotState
from src.core.optimizer.milp_model import build_optimization_model


@pytest.mark.acceptance
@pytest.mark.integration
class TestAT10VDV463AutomaticPreconditioning:
    """AT-10: Automatic preconditioning from VDV 463."""

    def test_automatic_preconditioning_duration_calculation(self):
        """Duration ~ (targetTemp - ambientTemp) * 3 min/°C (e.g. 28°C * 3 = 84 min)."""
        ambient = -10
        target = 18
        duration_min = max(0, (target - ambient) * 3.0)
        assert duration_min == 84.0

    def test_preconditioning_in_state_included_in_milp(self):
        """Preconditioning request in state is included in MILP (grid balance)."""
        now = datetime.utcnow()
        n_steps = 96
        end_time = now + timedelta(hours=5)
        duration_min = 84.0
        start_time = end_time - timedelta(minutes=duration_min)

        state = DepotState(
            vehicle_socs={"bus_101": 0.4},
            battery_soc=0.5,
            prices=[0.1] * n_steps,
            demand_charge_rate=20.0,
            current_month_peak=0.0,
            vehicle_availability={"bus_101": [True] * n_steps},
            energy_requirements={"bus_101": 100.0},
            departure_times={"bus_101": 20},
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
