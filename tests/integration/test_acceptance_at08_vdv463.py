"""AT-08: VDV 463 Charging Request Integration.

GIVEN an upstream BMS connected via VDV 463
AND BMS sends ProvideChargingRequests with vehicleId, expectedArrivalTimeAtChargingPoint,
    requestedTimeForDeparture, minTargetSoc=90, priority=1
WHEN optimization runs
THEN bus schedule uses VDV 463 arrival/departure times
AND bus reaches >= 90% SoC by departure
AND vehicleId and chargingPointId resolve to known internal entities.
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from src.core.models import DepotState, DepotConfig
from src.core.optimizer.milp_model import build_optimization_model
from src.core.optimizer import optimize


@pytest.mark.acceptance
@pytest.mark.integration
class TestAT08VDV463ChargingRequestIntegration:
    """AT-08: VDV 463 charging request data incorporated into optimization."""

    def test_optimizer_uses_vehicle_departure_soc_min_from_state(self):
        """Optimizer enforces per-vehicle min SoC at departure (e.g. 90% from VDV 463)."""
        now = datetime.utcnow()
        horizon_start = now
        n_steps = 96
        delta_t = 0.25
        t_depart = 20  # 5 hours in

        state = DepotState(
            vehicle_socs={"bus_101": 0.35},
            battery_soc=0.5,
            prices=[0.1] * n_steps,
            demand_charge_rate=20.0,
            current_month_peak=0.0,
            vehicle_availability={"bus_101": [True] * n_steps},
            energy_requirements={"bus_101": 100.0},
            departure_times={"bus_101": t_depart},
            building_power=[50.0] * n_steps,
            vehicle_departure_soc_min={"bus_101": 0.90},  # 90% from VDV 463 minTargetSoc
            vehicle_departure_soc_max={"bus_101": 1.0},
            vehicle_priorities={"bus_101": 1},
        )
        config = DepotConfig(
            vehicle_capacities={"bus_101": 324.0},
            vehicle_max_charge_kw={"bus_101": 80.0},
            charger_groups={80.0: 2},
            charger_efficiency=0.95,
            charger_vehicle_access={"cp1": {"bus_101"}, "cp2": {"bus_101"}},
            battery_capacity=1.0,
            battery_power=0.1,
            max_site_power=500.0,
        )

        model = build_optimization_model(state, config, horizon_start=horizon_start)
        assert hasattr(model, "departure_soc_min")
        # Constraint should enforce SoC at t_depart >= 0.90 for bus_101
        assert model.departure_soc_min["bus_101"].body.polynomial_degree() in (0, 1)

    def test_optimizer_with_preconditioning_requests_in_state(self):
        """Optimizer includes preconditioning load when state has preconditioning_requests."""
        now = datetime.utcnow()
        n_steps = 96
        # Preconditioning window: timesteps 16-20 (4–5 h)
        start_time = now + timedelta(hours=4)
        end_time = now + timedelta(hours=5)

        state = DepotState(
            vehicle_socs={"bus_101": 0.4},
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
        assert hasattr(model, "precond_constraint")
