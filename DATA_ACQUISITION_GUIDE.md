"""Data acquisition and simulator setup documentation."""

# Data Acquisition and Simulator Setup Guide

This document provides step-by-step instructions for acquiring real-world V2G datasets and setting up simulators for comprehensive testing of the Favonius Energy V2G system.

## Table of Contents

1. [Real-World Dataset Acquisition](#real-world-dataset-acquisition)
2. [Simulator Setup](#simulator-setup)
3. [Testing Workflows](#testing-workflows)
4. [Data Processing Pipelines](#data-processing-pipelines)
5. [Validation Procedures](#validation-procedures)

## Real-World Dataset Acquisition

### REVS Project Dataset

**Source:** REVS Project (Grid Contingency Response)
**URL:** [REVS Project](https://revs.berkeley.edu/)
**Description:** 16 V2G EVs responding to grid contingency events with high-resolution power meter data

#### Acquisition Steps:

1. **Access Request:**
   ```bash
   # Contact REVS project team
   # Email: revs@berkeley.edu
   # Request: V2G contingency response dataset
   ```

2. **Data Format:**
   - CSV files with 25ms resolution
   - Columns: timestamp, station_id, power_kw, voltage_v, current_a, frequency_hz, soc_percent
   - Time range: 2+ million hours of data

3. **Download Script:**
   ```python
   # scripts/download_revs_data.py
   import requests
   import pandas as pd
   
   def download_revs_dataset():
       # REVS dataset download logic
       # Note: Requires authentication
       pass
   ```

### TU Dortmund Dataset

**Source:** TU Dortmund High-Resolution EV Charging Dataset
**URL:** [TU Dortmund Dataset](https://www.tu-dortmund.de/)
**Description:** 142 EV charging profiles with bidirectional capability

#### Acquisition Steps:

1. **Access Request:**
   ```bash
   # Contact TU Dortmund research team
   # Request: High-resolution EV charging dataset
   ```

2. **Data Format:**
   - Profile types: Static V1G (28), Static V2G (11), Dynamic V1G (69), Dynamic V2G (34)
   - Columns: timestamp, profile_type, power_kw, voltage_v, current_a, soc_percent, efficiency, thd_percent
   - Resolution: 5-minute intervals

3. **Download Script:**
   ```python
   # scripts/download_tudortmund_data.py
   def download_tudortmund_dataset():
       # TU Dortmund dataset download logic
       pass
   ```

### Electric Nation Dataset

**Source:** Electric Nation V2G (UK)
**URL:** [Electric Nation](https://www.electricnation.org.uk/)
**Description:** 100 Nissan EV owners with V2G chargers

#### Acquisition Steps:

1. **Access Request:**
   ```bash
   # Contact Electric Nation project
   # Request: V2G charging behavior dataset
   ```

2. **Data Format:**
   - Tariff types: Standard, Time-of-Use, Dynamic, V2G Optimized
   - Columns: timestamp, station_id, tariff_type, power_kw, energy_kwh, is_peak, is_weekend
   - Time range: 2+ million hours

3. **Download Script:**
   ```python
   # scripts/download_electric_nation_data.py
   def download_electric_nation_dataset():
       # Electric Nation dataset download logic
       pass
   ```

### Parker Project Dataset

**Source:** Parker Project (Denmark)
**URL:** [Parker Project](https://parker-project.com/)
**Description:** V2G frequency regulation services

#### Acquisition Steps:

1. **Access Request:**
   ```bash
   # Contact Parker Project team
   # Request: V2G frequency regulation dataset
   ```

2. **Data Format:**
   - Frequency regulation data
   - Columns: timestamp, station_id, frequency_hz, power_kw, regulation_signal
   - Resolution: 1-second intervals

### University of Delaware / PJM Dataset

**Source:** University of Delaware / PJM Frequency Regulation
**URL:** [University of Delaware](https://www.udel.edu/)
**Description:** V2G frequency regulation in PJM market

#### Acquisition Steps:

1. **Access Request:**
   ```bash
   # Contact University of Delaware research team
   # Request: PJM frequency regulation dataset
   ```

2. **Data Format:**
   - PJM market data
   - Columns: timestamp, station_id, regulation_signal, power_kw, market_price
   - Resolution: 4-second intervals

## Simulator Setup

### CitrineOS Simulator

**Description:** OCA-certified OCPP 2.0.1 CSMS implementation

#### Setup Steps:

1. **Docker Installation:**
   ```bash
   # Install Docker
   sudo apt-get update
   sudo apt-get install docker.io
   
   # Install Docker Compose
   sudo apt-get install docker-compose
   ```

2. **CitrineOS Setup:**
   ```bash
   # Clone CitrineOS repository
   git clone https://github.com/OpenChargingCloud/CitrineOS.git
   cd CitrineOS
   
   # Start CitrineOS with Docker Compose
   docker-compose up -d
   ```

3. **Configuration:**
   ```yaml
   # docker-compose.yml
   version: '3.8'
   services:
     citrineos:
       image: citrineos/citrineos:latest
       ports:
         - "8080:8080"
       environment:
         - SPRING_PROFILES_ACTIVE=docker
   ```

4. **Test Connection:**
   ```python
   # tests/e2e/citrineos_simulator.py
   from tests.e2e.citrineos_simulator import CitrineOSSimulator
   
   async def test_citrineos_connection():
       simulator = CitrineOSSimulator("TEST_001", "ws://localhost:8080")
       await simulator.connect()
       await simulator.boot_notification()
       await simulator.disconnect()
   ```

### EVerest Simulator

**Description:** Hardware-grade charge point simulator

#### Setup Steps:

1. **EVerest Installation:**
   ```bash
   # Install EVerest
   git clone https://github.com/EVerest/EVerest.git
   cd EVerest
   
   # Install dependencies
   sudo apt-get install cmake build-essential
   
   # Build EVerest
   mkdir build && cd build
   cmake ..
   make
   ```

2. **Configuration:**
   ```yaml
   # everest_config.yaml
   active_modules:
     - ocpp
     - evse
     - energy_manager
   
   ocpp:
     implementation: ocpp201
     central_system_url: "ws://localhost:9000"
   ```

3. **Log Parsing:**
   ```python
   # scripts/parse_everest_logs.py
   def parse_everest_logs(log_file):
       # Parse EVerest log files
       # Extract OCPP messages
       pass
   ```

### MobileHouse OCPP Python Library

**Description:** Programmatic OCPP simulation

#### Setup Steps:

1. **Installation:**
   ```bash
   # Install MobileHouse OCPP
   pip install ocpp
   ```

2. **Basic Usage:**
   ```python
   # scripts/mobilehouse_simulator.py
   from ocpp.v201 import ChargePoint
   import asyncio
   
   class MobileHouseSimulator(ChargePoint):
       async def simulate_station(self):
           # Simulate charging station behavior
           pass
   ```

3. **Fleet Simulation:**
   ```python
   # scripts/mobilehouse_fleet.py
   async def simulate_fleet(num_stations=100):
       # Simulate multiple charging stations
       pass
   ```

### MicroOCPP Browser Simulator

**Description:** Browser-based OCPP simulator

#### Setup Steps:

1. **Web Interface:**
   ```bash
   # Access MicroOCPP simulator
   # URL: https://microocpp.com/simulator
   ```

2. **Configuration:**
   - Select OCPP version: 2.0.1
   - Set server URL: ws://localhost:9000
   - Configure station parameters

3. **Automation:**
   ```python
   # scripts/microocpp_automation.py
   from selenium import webdriver
   
   def automate_microocpp():
       # Automate browser simulator
       pass
   ```

## Testing Workflows

### Phase 1: OCPP Protocol Validation

1. **Message Coverage Testing:**
   ```bash
   # Run OCPP message tests
   python -m pytest tests/e2e/test_ocpp_messages.py -v
   ```

2. **Protocol Compliance:**
   ```bash
   # Run compliance tests
   python -m pytest tests/e2e/test_ocpp_compliance.py -v
   ```

3. **Security Testing:**
   ```bash
   # Run security tests
   python -m pytest tests/security/test_ocpp_security.py -v
   ```

### Phase 2: Algorithm Validation with Real Data

1. **Data Ingestion:**
   ```bash
   # Ingest real datasets
   python scripts/ingest_revs_data.py
   python scripts/ingest_tudortmund_data.py
   python scripts/ingest_electric_nation_data.py
   ```

2. **Optimization Testing:**
   ```bash
   # Test optimization algorithms
   python -m pytest tests/integration/test_optimization_algorithms.py -v
   ```

3. **Performance Validation:**
   ```bash
   # Validate performance metrics
   python -m pytest tests/integration/test_performance_validation.py -v
   ```

### Phase 3: Scale Testing

1. **Load Testing:**
   ```bash
   # Run load tests
   python -m pytest tests/load/test_scale_performance.py -v
   ```

2. **Fleet Simulation:**
   ```bash
   # Simulate large fleets
   python scripts/simulate_large_fleet.py --stations 1000
   ```

3. **Stress Testing:**
   ```bash
   # Run stress tests
   python -m pytest tests/load/test_stress_scenarios.py -v
   ```

### Phase 4: Certification Preparation

1. **OCTT Testing:**
   ```bash
   # Run OCTT compliance tests
   python scripts/run_octt_tests.py
   ```

2. **Certification Documentation:**
   ```bash
   # Generate certification reports
   python scripts/generate_certification_report.py
   ```

## Data Processing Pipelines

### Data Ingestion Pipeline

```python
# scripts/data_ingestion_pipeline.py
import pandas as pd
from datetime import datetime

class DataIngestionPipeline:
    def __init__(self, timescale_client):
        self.timescale_client = timescale_client
    
    async def ingest_revs_data(self, csv_file):
        # Ingest REVS dataset
        df = pd.read_csv(csv_file)
        
        for _, row in df.iterrows():
            await self.timescale_client.store_telemetry_data({
                "timestamp": row["timestamp"],
                "station_id": row["station_id"],
                "power_kw": row["power_kw"],
                "voltage_v": row["voltage_v"],
                "current_a": row["current_a"],
                "frequency_hz": row["frequency_hz"],
                "soc_percent": row["soc_percent"]
            })
    
    async def ingest_tudortmund_data(self, csv_file):
        # Ingest TU Dortmund dataset
        df = pd.read_csv(csv_file)
        
        for _, row in df.iterrows():
            await self.timescale_client.store_charging_profile({
                "timestamp": row["timestamp"],
                "station_id": row["station_id"],
                "profile_type": row["profile_type"],
                "power_kw": row["power_kw"],
                "voltage_v": row["voltage_v"],
                "current_a": row["current_a"],
                "soc_percent": row["soc_percent"],
                "efficiency": row["efficiency"],
                "thd_percent": row["thd_percent"]
            })
    
    async def ingest_electric_nation_data(self, csv_file):
        # Ingest Electric Nation dataset
        df = pd.read_csv(csv_file)
        
        for _, row in df.iterrows():
            await self.timescale_client.store_transaction({
                "timestamp": row["timestamp"],
                "station_id": row["station_id"],
                "tariff_type": row["tariff_type"],
                "power_kw": row["power_kw"],
                "energy_kwh": row["energy_kwh"],
                "is_peak": row["is_peak"],
                "is_weekend": row["is_weekend"]
            })
```

### Data Validation Pipeline

```python
# scripts/data_validation_pipeline.py
class DataValidationPipeline:
    def __init__(self, timescale_client):
        self.timescale_client = timescale_client
    
    async def validate_revs_data(self):
        # Validate REVS data quality
        data = await self.timescale_client.get_telemetry_data()
        
        # Check data completeness
        assert len(data) > 0, "No REVS data found"
        
        # Check data quality
        for record in data:
            assert record["power_kw"] is not None, "Missing power data"
            assert record["soc_percent"] is not None, "Missing SOC data"
            assert 0 <= record["soc_percent"] <= 100, "Invalid SOC range"
    
    async def validate_tudortmund_data(self):
        # Validate TU Dortmund data quality
        profiles = await self.timescale_client.get_charging_profiles()
        
        # Check profile distribution
        profile_types = {}
        for profile in profiles:
            profile_type = profile["profile_type"]
            profile_types[profile_type] = profile_types.get(profile_type, 0) + 1
        
        assert profile_types["static_v1g"] == 28, "Incorrect static V1G count"
        assert profile_types["static_v2g"] == 11, "Incorrect static V2G count"
        assert profile_types["dynamic_v1g"] == 69, "Incorrect dynamic V1G count"
        assert profile_types["dynamic_v2g"] == 34, "Incorrect dynamic V2G count"
    
    async def validate_electric_nation_data(self):
        # Validate Electric Nation data quality
        transactions = await self.timescale_client.get_transactions()
        
        # Check tariff distribution
        tariff_types = {}
        for transaction in transactions:
            tariff_type = transaction["tariff_type"]
            tariff_types[tariff_type] = tariff_types.get(tariff_type, 0) + 1
        
        assert len(tariff_types) == 4, "Should have 4 tariff types"
        assert "v2g_optimized" in tariff_types, "Missing V2G optimized tariff"
```

## Validation Procedures

### Dataset Validation

1. **Data Quality Checks:**
   ```bash
   # Run data quality validation
   python scripts/validate_dataset_quality.py
   ```

2. **Completeness Validation:**
   ```bash
   # Check data completeness
   python scripts/validate_data_completeness.py
   ```

3. **Consistency Validation:**
   ```bash
   # Validate data consistency
   python scripts/validate_data_consistency.py
   ```

### Simulator Validation

1. **Connection Testing:**
   ```bash
   # Test simulator connections
   python scripts/test_simulator_connections.py
   ```

2. **Message Validation:**
   ```bash
   # Validate simulator messages
   python scripts/validate_simulator_messages.py
   ```

3. **Performance Testing:**
   ```bash
   # Test simulator performance
   python scripts/test_simulator_performance.py
   ```

### System Validation

1. **End-to-End Testing:**
   ```bash
   # Run end-to-end tests
   python -m pytest tests/e2e/ -v
   ```

2. **Integration Testing:**
   ```bash
   # Run integration tests
   python -m pytest tests/integration/ -v
   ```

3. **Load Testing:**
   ```bash
   # Run load tests
   python -m pytest tests/load/ -v
   ```

## Troubleshooting

### Common Issues

1. **Dataset Access Denied:**
   - Contact dataset providers for access permissions
   - Check authentication credentials
   - Verify data usage agreements

2. **Simulator Connection Failures:**
   - Check network connectivity
   - Verify server URLs and ports
   - Review firewall settings

3. **Data Processing Errors:**
   - Validate data formats
   - Check for missing values
   - Verify data types

### Support Resources

- **Documentation:** [System Documentation](SYSTEM_DOCUMENTATION.md)
- **Testing Summary:** [Testing Summary](TESTING_SUMMARY.md)
- **Test Results:** [Test Results](TEST_RESULTS.md)
- **Data Sources:** [EV Charging Data Sources](EV%20Charging%20Data%20Sources.md)

## Conclusion

This guide provides comprehensive instructions for acquiring real-world V2G datasets and setting up simulators for thorough testing of the Favonius Energy V2G system. Follow the steps carefully to ensure proper data acquisition and simulator setup for comprehensive testing.
