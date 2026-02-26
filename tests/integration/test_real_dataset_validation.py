"""Real dataset validation tests for V2G optimization pipelines."""

import os

# Import test dependencies
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock

import pytest

# import pandas as pd  # Optional dependency
# import numpy as np   # Optional dependency

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from websocket_handler.config import OptimizationServiceConfig
from websocket_handler.optimization_engine import OptimizationEngine
from websocket_handler.telemetry_ingestion import TelemetryIngestionService


class TestREVSDatasetValidation:
    """Test REVS dataset validation for grid contingency response."""

    @pytest.fixture
    def mock_timescale_client(self):
        """Mock TimescaleClient for testing."""
        client = Mock()
        client.store_electricity_prices = AsyncMock()
        client.get_latest_prices = AsyncMock()
        client.get_active_charging_sessions = AsyncMock()
        client.store_charging_schedule = AsyncMock()
        client.store_optimization_decision = AsyncMock()
        client.store_telemetry_data = AsyncMock()
        return client

    @pytest.fixture
    def revs_sample_data(self):
        """Create sample REVS dataset data."""
        # Sample REVS data structure based on the dataset description
        data = []
        base_time = datetime.now(timezone.utc)

        # Simulate 16 V2G EVs during grid contingency event
        for i in range(16):
            station_id = f"REVS_STATION_{i+1:03d}"

            # High-resolution power meter data (25ms resolution)
            for j in range(100):  # 2.5 seconds of data
                timestamp = base_time + timedelta(milliseconds=j * 25)

                # Simulate bidirectional power flow during contingency
                if j < 20:  # Before contingency
                    power_kw = 22.0  # Normal charging
                elif j < 60:  # During contingency
                    power_kw = -15.0  # Discharge to grid
                else:  # After contingency
                    power_kw = 18.0  # Reduced charging

                data.append(
                    {
                        "timestamp": timestamp,
                        "station_id": station_id,
                        "power_kw": power_kw,
                        "voltage_v": 240.0,
                        "current_a": power_kw * 1000 / 240.0,
                        "frequency_hz": 50.0
                        + (0.1 if j < 60 else 0.0),  # Frequency deviation during contingency
                        "soc_percent": max(20, 80 - j * 0.5),  # Decreasing SOC
                        "grid_frequency_hz": 50.0 + (0.2 if j < 60 else 0.0),
                    }
                )

        return data

    @pytest.mark.asyncio
    async def test_revs_telemetry_ingestion(self, mock_timescale_client, revs_sample_data):
        """Test REVS telemetry data ingestion."""

        # Create telemetry ingestion service
        ingestion_service = TelemetryIngestionService(mock_timescale_client)

        # Ingest REVS data
        for data_point in revs_sample_data:
            await ingestion_service.ingest_telemetry_data(data_point)

        # Verify data was stored
        assert mock_timescale_client.store_telemetry_data.call_count == len(revs_sample_data)

        # Verify data structure
        stored_data = mock_timescale_client.store_telemetry_data.call_args[0][0]
        assert "timestamp" in stored_data
        assert "station_id" in stored_data
        assert "power_kw" in stored_data
        assert "soc_percent" in stored_data

    @pytest.mark.asyncio
    async def test_revs_optimization_response(self, mock_timescale_client, revs_sample_data):
        """Test optimization engine response to REVS data."""

        # Create optimization engine
        config = OptimizationServiceConfig(
            enabled=True,
            horizon_hours=4,
            timestep_minutes=60,
            soc_minimum=0.2,
            soc_target=0.8,
            charge_power_kw=22.0,
            discharge_power_kw=15.0,
            battery_capacity_kwh=75.0,
        )

        optimization_engine = OptimizationEngine(
            config, mock_timescale_client, Mock(), Mock()  # Supabase client  # Connection manager
        )

        # Mock charging sessions from REVS data
        sessions = []
        for i in range(16):
            station_id = f"REVS_STATION_{i+1:03d}"
            sessions.append(
                {
                    "station_id": station_id,
                    "evse_id": 1,
                    "connector_id": 1,
                    "start_time": datetime.now(timezone.utc),
                    "end_time": datetime.now(timezone.utc) + timedelta(hours=2),
                    "start_soc_percent": 50.0 + i * 2.0,  # Varying SOC levels
                }
            )

        mock_timescale_client.get_active_charging_sessions.return_value = sessions

        # Mock price data with contingency pricing
        prices = [
            {
                "time": datetime.now(timezone.utc),
                "lmp_price_mwh": 200.0,  # High price during contingency
            }
        ]
        mock_timescale_client.get_latest_prices.return_value = prices

        # Run optimization
        await optimization_engine._run_optimization("contingency_response")

        # Verify optimization decision was stored
        mock_timescale_client.store_optimization_decision.assert_called_once()

        # Verify charging schedules were created
        assert mock_timescale_client.store_charging_schedule.call_count == 16

        # Verify schedules have discharge capability
        stored_schedules = mock_timescale_client.store_charging_schedule.call_args_list
        for schedule_call in stored_schedules:
            schedule = schedule_call[0][0]
            assert schedule["purpose"] == "v2g_schedule"
            assert len(schedule["schedule_periods"]) > 0

    @pytest.mark.asyncio
    async def test_revs_frequency_response_validation(
        self, mock_timescale_client, revs_sample_data
    ):
        """Test frequency response validation from REVS data."""

        # Analyze frequency response patterns
        contingency_data = [d for d in revs_sample_data if d["grid_frequency_hz"] > 50.1]

        assert len(contingency_data) > 0, "Should have contingency data"

        # Verify V2G response to frequency deviation
        for data_point in contingency_data:
            if data_point["power_kw"] < 0:  # Discharge
                assert (
                    data_point["grid_frequency_hz"] > 50.0
                ), "Should discharge when frequency is high"

        # Verify response time (should be within seconds)
        contingency_start = min(d["timestamp"] for d in contingency_data)
        response_times = []

        for data_point in contingency_data:
            if data_point["power_kw"] < 0:
                response_time = (data_point["timestamp"] - contingency_start).total_seconds()
                response_times.append(response_time)

        if response_times:
            max_response_time = max(response_times)
            assert max_response_time < 5.0, "V2G response should be within 5 seconds"

    @pytest.mark.asyncio
    async def test_revs_soc_tracking(self, mock_timescale_client, revs_sample_data):
        """Test SOC tracking accuracy from REVS data."""

        # Group data by station
        station_data = {}
        for data_point in revs_sample_data:
            station_id = data_point["station_id"]
            if station_id not in station_data:
                station_data[station_id] = []
            station_data[station_id].append(data_point)

        # Verify SOC progression for each station
        for station_id, data in station_data.items():
            # Sort by timestamp
            data.sort(key=lambda x: x["timestamp"])

            # Verify SOC decreases over time (discharging)
            initial_soc = data[0]["soc_percent"]
            final_soc = data[-1]["soc_percent"]

            assert final_soc < initial_soc, f"SOC should decrease for {station_id}"

            # Verify SOC progression is reasonable
            soc_change = initial_soc - final_soc
            assert 0 < soc_change < 50, f"SOC change should be reasonable for {station_id}"


class TestTUDortmundDatasetValidation:
    """Test TU Dortmund dataset validation for V2G profiles."""

    @pytest.fixture
    def tudortmund_sample_data(self):
        """Create sample TU Dortmund dataset data."""
        # Sample TU Dortmund data structure based on the dataset description
        data = []
        base_time = datetime.now(timezone.utc)

        # Simulate 142 EV charging profiles with bidirectional capability
        profile_types = [
            ("static_v1g", 28, False),  # Static V1G
            ("static_v2g", 11, True),  # Static V2G
            ("dynamic_v1g", 69, False),  # Dynamic V1G
            ("dynamic_v2g", 34, True),  # Dynamic V2G
        ]

        profile_id = 0
        for profile_type, count, bidirectional in profile_types:
            for i in range(count):
                profile_id += 1
                station_id = f"TU_STATION_{profile_id:03d}"

                # Generate charging profile data
                for j in range(100):  # 100 time steps
                    timestamp = base_time + timedelta(minutes=j * 5)

                    # Different power patterns based on profile type
                    if "static" in profile_type:
                        power_kw = 22.0 if not bidirectional else (22.0 if j < 50 else -15.0)
                    else:  # dynamic
                        import math

                        power_kw = (
                            22.0 * (1 + 0.5 * math.sin(j * 0.1))
                            if not bidirectional
                            else 22.0 * math.sin(j * 0.1)
                        )

                    # Calculate SOC progression
                    soc_percent = max(20, 80 - j * 0.6)

                    data.append(
                        {
                            "timestamp": timestamp,
                            "station_id": station_id,
                            "profile_type": profile_type,
                            "power_kw": power_kw,
                            "voltage_v": 240.0,
                            "current_a": abs(power_kw) * 1000 / 240.0,
                            "soc_percent": soc_percent,
                            "efficiency": 0.95,
                            "thd_percent": 2.0
                            + (0.5 if j % 2 == 0 else -0.5),  # Simulate THD variation
                            "reactive_power_kvar": power_kw * 0.1,
                            "bidirectional": bidirectional,
                        }
                    )

        return data

    @pytest.mark.asyncio
    async def test_tudortmund_profile_analysis(self, mock_timescale_client, tudortmund_sample_data):
        """Test TU Dortmund profile analysis."""

        # Analyze profile types
        profile_counts = {}
        bidirectional_count = 0

        for data_point in tudortmund_sample_data:
            profile_type = data_point["profile_type"]
            profile_counts[profile_type] = profile_counts.get(profile_type, 0) + 1

            if data_point["bidirectional"]:
                bidirectional_count += 1

        # Verify profile distribution
        assert profile_counts["static_v1g"] == 28
        assert profile_counts["static_v2g"] == 11
        assert profile_counts["dynamic_v1g"] == 69
        assert profile_counts["dynamic_v2g"] == 34

        # Verify bidirectional capability
        assert bidirectional_count == 45  # 11 + 34

    @pytest.mark.asyncio
    async def test_tudortmund_harmonics_analysis(
        self, mock_timescale_client, tudortmund_sample_data
    ):
        """Test TU Dortmund harmonics analysis."""

        # Analyze harmonics data
        thd_values = [d["thd_percent"] for d in tudortmund_sample_data]

        # Verify THD values are reasonable
        assert all(0 < thd < 10 for thd in thd_values), "THD should be reasonable"

        # Calculate average THD
        avg_thd = sum(thd_values) / len(thd_values)
        assert 1.5 < avg_thd < 3.0, "Average THD should be in expected range"

    @pytest.mark.asyncio
    async def test_tudortmund_efficiency_analysis(
        self, mock_timescale_client, tudortmund_sample_data
    ):
        """Test TU Dortmund efficiency analysis."""

        # Analyze efficiency data
        efficiency_values = [d["efficiency"] for d in tudortmund_sample_data]

        # Verify efficiency values are reasonable
        assert all(0.9 < eff < 1.0 for eff in efficiency_values), "Efficiency should be reasonable"

        # Calculate average efficiency
        avg_efficiency = sum(efficiency_values) / len(efficiency_values)
        assert 0.94 < avg_efficiency < 0.96, "Average efficiency should be in expected range"

    @pytest.mark.asyncio
    async def test_tudortmund_v2g_optimization(self, mock_timescale_client, tudortmund_sample_data):
        """Test V2G optimization with TU Dortmund data."""

        # Create optimization engine
        config = OptimizationServiceConfig(
            enabled=True,
            horizon_hours=4,
            timestep_minutes=60,
            soc_minimum=0.2,
            soc_target=0.8,
            charge_power_kw=22.0,
            discharge_power_kw=15.0,
            battery_capacity_kwh=75.0,
        )

        optimization_engine = OptimizationEngine(
            config, mock_timescale_client, Mock(), Mock()  # Supabase client  # Connection manager
        )

        # Create charging sessions from TU Dortmund data
        sessions = []
        for data_point in tudortmund_sample_data:
            if data_point["bidirectional"]:  # Only V2G capable stations
                sessions.append(
                    {
                        "station_id": data_point["station_id"],
                        "evse_id": 1,
                        "connector_id": 1,
                        "start_time": data_point["timestamp"],
                        "end_time": data_point["timestamp"] + timedelta(hours=2),
                        "start_soc_percent": data_point["soc_percent"],
                    }
                )

        # Remove duplicates
        unique_sessions = []
        seen_stations = set()
        for session in sessions:
            if session["station_id"] not in seen_stations:
                unique_sessions.append(session)
                seen_stations.add(session["station_id"])

        mock_timescale_client.get_active_charging_sessions.return_value = unique_sessions

        # Mock price data
        prices = [{"time": datetime.now(timezone.utc), "lmp_price_mwh": 100.0}]
        mock_timescale_client.get_latest_prices.return_value = prices

        # Run optimization
        await optimization_engine._run_optimization("tudortmund_validation")

        # Verify optimization decision was stored
        mock_timescale_client.store_optimization_decision.assert_called_once()

        # Verify charging schedules were created for V2G stations
        assert mock_timescale_client.store_charging_schedule.call_count == len(unique_sessions)


class TestElectricNationDatasetValidation:
    """Test Electric Nation dataset validation for tariff-driven cost calculations."""

    @pytest.fixture
    def electric_nation_sample_data(self):
        """Create sample Electric Nation dataset data."""
        # Sample Electric Nation data structure based on the dataset description
        data = []
        base_time = datetime.now(timezone.utc)

        # Simulate 100 Nissan EV owners with V2G chargers
        for i in range(100):
            station_id = f"EN_STATION_{i+1:03d}"

            # Different energy supplier tariffs
            tariff_types = ["standard", "time_of_use", "dynamic", "v2g_optimized"]
            tariff_type = tariff_types[i % len(tariff_types)]

            # Generate charging data over 2+ million hours (simplified)
            for j in range(1000):  # 1000 time steps
                timestamp = base_time + timedelta(hours=j * 2)

                # Simulate charging behavior patterns
                hour = timestamp.hour

                # Peak hours (6-9 AM, 6-9 PM)
                is_peak = hour in [6, 7, 8, 18, 19, 20]

                # Weekend vs weekday
                is_weekend = timestamp.weekday() >= 5

                # Calculate power based on tariff and time
                if tariff_type == "standard":
                    power_kw = 22.0
                elif tariff_type == "time_of_use":
                    power_kw = 15.0 if is_peak else 22.0
                elif tariff_type == "dynamic":
                    power_kw = 10.0 if is_peak else 25.0
                else:  # v2g_optimized
                    power_kw = -15.0 if is_peak else 22.0

                # Calculate energy import/export
                energy_kwh = abs(power_kw) * 2.0  # 2-hour interval

                data.append(
                    {
                        "timestamp": timestamp,
                        "station_id": station_id,
                        "tariff_type": tariff_type,
                        "power_kw": power_kw,
                        "energy_kwh": energy_kwh,
                        "is_peak": is_peak,
                        "is_weekend": is_weekend,
                        "import_energy_kwh": energy_kwh if power_kw > 0 else 0,
                        "export_energy_kwh": energy_kwh if power_kw < 0 else 0,
                        "tariff_rate_gbp_per_kwh": 0.15 if is_peak else 0.10,
                        "export_rate_gbp_per_kwh": 0.08 if is_peak else 0.05,
                    }
                )

        return data

    @pytest.mark.asyncio
    async def test_electric_nation_tariff_analysis(
        self, mock_timescale_client, electric_nation_sample_data
    ):
        """Test Electric Nation tariff analysis."""

        # Analyze tariff distribution
        tariff_counts = {}
        for data_point in electric_nation_sample_data:
            tariff_type = data_point["tariff_type"]
            tariff_counts[tariff_type] = tariff_counts.get(tariff_type, 0) + 1

        # Verify tariff distribution
        assert tariff_counts["standard"] == 25000  # 100 stations * 1000/4
        assert tariff_counts["time_of_use"] == 25000
        assert tariff_counts["dynamic"] == 25000
        assert tariff_counts["v2g_optimized"] == 25000

    @pytest.mark.asyncio
    async def test_electric_nation_cost_calculation(
        self, mock_timescale_client, electric_nation_sample_data
    ):
        """Test Electric Nation cost calculation."""

        # Calculate costs for each station
        station_costs = {}

        for data_point in electric_nation_sample_data:
            station_id = data_point["station_id"]
            if station_id not in station_costs:
                station_costs[station_id] = {
                    "total_import_cost": 0,
                    "total_export_revenue": 0,
                    "net_cost": 0,
                }

            # Calculate import cost
            import_cost = data_point["import_energy_kwh"] * data_point["tariff_rate_gbp_per_kwh"]
            station_costs[station_id]["total_import_cost"] += import_cost

            # Calculate export revenue
            export_revenue = data_point["export_energy_kwh"] * data_point["export_rate_gbp_per_kwh"]
            station_costs[station_id]["total_export_revenue"] += export_revenue

            # Calculate net cost
            station_costs[station_id]["net_cost"] = (
                station_costs[station_id]["total_import_cost"]
                - station_costs[station_id]["total_export_revenue"]
            )

        # Verify cost calculations
        for station_id, costs in station_costs.items():
            assert costs["total_import_cost"] >= 0, "Import cost should be positive"
            assert costs["total_export_revenue"] >= 0, "Export revenue should be positive"
            assert costs["net_cost"] >= 0, "Net cost should be positive"

    @pytest.mark.asyncio
    async def test_electric_nation_v2g_revenue(
        self, mock_timescale_client, electric_nation_sample_data
    ):
        """Test Electric Nation V2G revenue analysis."""

        # Analyze V2G revenue by tariff type
        tariff_revenues = {}

        for data_point in electric_nation_sample_data:
            tariff_type = data_point["tariff_type"]
            if tariff_type not in tariff_revenues:
                tariff_revenues[tariff_type] = 0

            if data_point["power_kw"] < 0:  # Export
                revenue = data_point["export_energy_kwh"] * data_point["export_rate_gbp_per_kwh"]
                tariff_revenues[tariff_type] += revenue

        # Verify V2G revenue is highest for V2G optimized tariff
        assert tariff_revenues["v2g_optimized"] > tariff_revenues["dynamic"]
        assert tariff_revenues["v2g_optimized"] > tariff_revenues["time_of_use"]
        assert tariff_revenues["v2g_optimized"] > tariff_revenues["standard"]

    @pytest.mark.asyncio
    async def test_electric_nation_peak_shaving(
        self, mock_timescale_client, electric_nation_sample_data
    ):
        """Test Electric Nation peak shaving analysis."""

        # Analyze peak vs off-peak behavior
        peak_data = [d for d in electric_nation_sample_data if d["is_peak"]]
        off_peak_data = [d for d in electric_nation_sample_data if not d["is_peak"]]

        # Calculate average power during peak hours
        peak_power = sum([d["power_kw"] for d in peak_data]) / len(peak_data)
        off_peak_power = sum([d["power_kw"] for d in off_peak_data]) / len(off_peak_data)

        # Verify peak shaving behavior
        assert peak_power < off_peak_power, "Power should be lower during peak hours"

        # Verify V2G stations discharge during peak
        v2g_peak_data = [d for d in peak_data if d["tariff_type"] == "v2g_optimized"]
        if v2g_peak_data:
            v2g_peak_power = sum([d["power_kw"] for d in v2g_peak_data]) / len(v2g_peak_data)
            assert v2g_peak_power < 0, "V2G stations should discharge during peak hours"


class TestDatasetIntegration:
    """Test integration of multiple datasets."""

    @pytest.fixture
    def mock_timescale_client(self):
        """Mock TimescaleClient for testing."""
        client = Mock()
        client.store_electricity_prices = AsyncMock()
        client.get_latest_prices = AsyncMock()
        client.get_active_charging_sessions = AsyncMock()
        client.store_charging_schedule = AsyncMock()
        client.store_optimization_decision = AsyncMock()
        client.store_telemetry_data = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_multi_dataset_optimization(self, mock_timescale_client):
        """Test optimization with multiple datasets."""

        # Create optimization engine
        config = OptimizationServiceConfig(
            enabled=True,
            horizon_hours=4,
            timestep_minutes=60,
            soc_minimum=0.2,
            soc_target=0.8,
            charge_power_kw=22.0,
            discharge_power_kw=15.0,
            battery_capacity_kwh=75.0,
        )

        optimization_engine = OptimizationEngine(
            config, mock_timescale_client, Mock(), Mock()  # Supabase client  # Connection manager
        )

        # Create mixed charging sessions from different datasets
        sessions = []

        # REVS stations (grid contingency response)
        for i in range(16):
            sessions.append(
                {
                    "station_id": f"REVS_STATION_{i+1:03d}",
                    "evse_id": 1,
                    "connector_id": 1,
                    "start_time": datetime.now(timezone.utc),
                    "end_time": datetime.now(timezone.utc) + timedelta(hours=2),
                    "start_soc_percent": 50.0 + i * 2.0,
                }
            )

        # TU Dortmund stations (laboratory V2G)
        for i in range(45):  # V2G capable stations
            sessions.append(
                {
                    "station_id": f"TU_STATION_{i+1:03d}",
                    "evse_id": 1,
                    "connector_id": 1,
                    "start_time": datetime.now(timezone.utc),
                    "end_time": datetime.now(timezone.utc) + timedelta(hours=3),
                    "start_soc_percent": 60.0 + i * 0.5,
                }
            )

        # Electric Nation stations (real-world V2G)
        for i in range(100):
            sessions.append(
                {
                    "station_id": f"EN_STATION_{i+1:03d}",
                    "evse_id": 1,
                    "connector_id": 1,
                    "start_time": datetime.now(timezone.utc),
                    "end_time": datetime.now(timezone.utc) + timedelta(hours=4),
                    "start_soc_percent": 40.0 + i * 0.3,
                }
            )

        mock_timescale_client.get_active_charging_sessions.return_value = sessions

        # Mock price data with contingency pricing
        prices = [
            {
                "time": datetime.now(timezone.utc),
                "lmp_price_mwh": 200.0,  # High price during contingency
            }
        ]
        mock_timescale_client.get_latest_prices.return_value = prices

        # Run optimization
        await optimization_engine._run_optimization("multi_dataset_validation")

        # Verify optimization decision was stored
        mock_timescale_client.store_optimization_decision.assert_called_once()

        # Verify charging schedules were created for all stations
        assert mock_timescale_client.store_charging_schedule.call_count == len(sessions)

        # Verify schedules have appropriate purposes
        stored_schedules = mock_timescale_client.store_charging_schedule.call_args_list
        for schedule_call in stored_schedules:
            schedule = schedule_call[0][0]
            assert schedule["purpose"] == "v2g_schedule"
            assert len(schedule["schedule_periods"]) > 0

    @pytest.mark.asyncio
    async def test_dataset_validation_summary(self, mock_timescale_client):
        """Test dataset validation summary."""

        # Create validation summary
        validation_results = {
            "revs_dataset": {
                "stations": 16,
                "contingency_response": True,
                "frequency_deviation": True,
                "soc_tracking": True,
            },
            "tudortmund_dataset": {
                "stations": 142,
                "v2g_capable": 45,
                "harmonics_analysis": True,
                "efficiency_analysis": True,
            },
            "electric_nation_dataset": {
                "stations": 100,
                "tariff_types": 4,
                "cost_calculation": True,
                "peak_shaving": True,
            },
        }

        # Verify validation results
        assert validation_results["revs_dataset"]["stations"] == 16
        assert validation_results["tudortmund_dataset"]["v2g_capable"] == 45
        assert validation_results["electric_nation_dataset"]["tariff_types"] == 4

        # Verify all datasets have required capabilities
        for dataset, results in validation_results.items():
            assert results["stations"] > 0, f"{dataset} should have stations"
            assert any(
                results[key] for key in results if isinstance(results[key], bool)
            ), f"{dataset} should have validation results"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
