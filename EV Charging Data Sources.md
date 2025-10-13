# EV Charger Datasets for V2G Platform Testing

Finding the right test data for OCPP 2.0/2.1 platforms has been challenging until recently, but **15+ open-source implementations, 8 active simulators, and multiple research datasets with bidirectional charging data are now publicly available**. This research identified immediately usable sources for both raw OCPP WebSocket messages and processed telemetry data, with several production-ready solutions deployable in under 15 minutes.

## Raw OCPP 2.0/2.1 message sources dominate the landscape

The ecosystem for OCPP 2.0/2.1 testing has matured dramatically in the past two years. **CitrineOS and EVerest represent the gold standard**—both are OCA-certified implementations that generate, log, and expose complete OCPP 2.0.1 WebSocket messages including all the message types your platform requires. CitrineOS provides a complete CSMS stack with PostgreSQL storage and a Data API for subscribing to live OCPP messages, while EVerest offers production-grade C++ charge point simulation with comprehensive logging to `/tmp/everest_ocpp_logs/`.

For instant testing without installation, **MicroOCPP's browser-based simulator** runs WebAssembly code directly in your browser and supports 15+ CSMS backends. The simulator generates real OCPP 2.0.1 messages visible through browser DevTools, making it ideal for quick protocol verification. If you need scriptable testing at scale, the **MobileHouse Python OCPP library** (MIT licensed) provides the most popular development framework with async/await support and extensive examples for generating all OCPP message types.

### CitrineOS: Production-ready CSMS with message logging
- **Repository**: https://github.com/citrineos/citrineos-core
- **OCPP support**: 2.0.1 (OCA certified), 1.6
- **Installation**: One Docker Compose command deploys the full stack
- **Message access**: PostgreSQL database + Data API subscription endpoint (`localhost:8080/data/ocpprouter/subscription`)
- **Format**: Raw JSON WebSocket frames [MessageType, CallID, Action, Payload]
- **Scale**: Production-ready, supports thousands of concurrent connections
- **Ports**: 8080 (HTTP API), 8081 (WebSocket no auth), 8082 (WebSocket with auth)
- **License**: Apache 2.0
- **Documentation**: https://citrineos.github.io/

**Quick setup**:
```bash
git clone https://github.com/citrineos/citrineos-core
cd citrineos-core/Server
docker-compose up -d
# Access Swagger docs at http://localhost:8080/docs
```

### EVerest: Hardware-grade charge point simulation
- **Repository**: https://github.com/EVerest/libocpp
- **OCPP support**: 2.0.1 (OCA certified), 2.1 (in development, Q1 2025)
- **Installation**: One-command demo with visualization
- **Message access**: Log files in `/tmp/everest_ocpp_logs/` directory
- **Format**: Multiple log formats, downloadable via Docker
- **Scale**: Production-grade C++ suitable for microcontrollers to data centers
- **Integration**: Includes Node-RED visualization dashboard
- **License**: Apache 2.0
- **Documentation**: https://everest.github.io/

**Quick setup**:
```bash
curl https://raw.githubusercontent.com/everest/everest-demo/main/demo-iso15118-2-ocpp-201.sh | bash -s -- -c -1
# Access UI at http://127.0.0.1:1880/ui
# Extract logs: docker cp everest-manager:/tmp/everest_ocpp_logs/ ./logs/
```

### MobileHouse OCPP Python library
- **Repository**: https://github.com/mobilityhouse/ocpp
- **OCPP support**: 1.6 (errata v4), 2.0.1 (Edition 2 & 3)
- **Installation**: `pip install ocpp`
- **Message generation**: Full async/await implementation with validation
- **Format**: Python objects serializable to JSON WebSocket format
- **Use case**: Custom simulator development, automated testing
- **License**: MIT
- **Documentation**: https://ocpp.readthedocs.io/

**Example BootNotification**:
```python
[2, "12345", "BootNotification", {
    "chargingStation": {
        "model": "Wallbox XYZ",
        "vendorName": "vendor"
    },
    "reason": "PowerUp"
}]
```

### MicroOCPP browser simulator
- **Website**: https://www.micro-ocpp.com/
- **OCPP support**: 1.6, 2.0.1 (alpha)
- **Installation**: None—runs in browser via WebAssembly
- **Message access**: Browser DevTools Network tab (WebSocket frames)
- **Features**: GUI-based charge point simulation, tested with 15+ CSMS backends
- **Use case**: Instant testing, protocol verification, no hardware needed
- **License**: MIT

### Additional OCPP implementations worth noting

**MaEVe CSMS** (Go, ThoughtWorks): Experimental CSMS with ISO 15118 Plug & Charge support. Includes Firestore persistence and MQTT broker. Repository: https://github.com/thoughtworks/maeve-csms

**OpenChargingCloud WWCP** (C#/.NET): Full OCPP 2.1 support with German Calibration Law compliance. Repository: https://github.com/OpenChargingCloud/WWCP_OCPP

**typed-ocpp** (TypeScript): Generated from official OCA JSON schemas with type-aware validation. Supports OCPP 2.1. Repository: https://github.com/jacoscaz/typed-ocpp

**Charge Point Simulator via OCPP 2.0.1** (Java): Docker image available, simulates multiple charge points with automated BootNotification and Heartbeats. Repository: https://github.com/extrawest/Charge-Point-Simulator-via-OCPP-2.0.1

## Real-world V2G datasets provide bidirectional power flow telemetry

While OCPP simulators address protocol testing, **three open-access research datasets provide actual V2G telemetry** with the power flow, SoC, and grid measurements your platform needs for integration testing. These datasets capture real bidirectional charging with positive/negative power values, state of charge progression, and grid frequency response—exactly what V2G optimization algorithms require.

### REVS Project (Australia): Grid contingency response data
- **Source**: Australian National University, Zenodo
- **DOI**: 10.5281/zenodo.11195210
- **Dataset URL**: https://zenodo.org/record/11195210
- **Content**: Real-world frequency contingency response from 16 V2G EVs during actual national grid event (February 13, 2024)
- **Measurements**:
  - High-speed power meter data (25ms resolution)
  - Charger power measurements (30s resolution)
  - Grid frequency during contingency event
  - Bidirectional power flows (charge/discharge)
- **Scale**: 51 Nissan LEAF vehicles, 51 bidirectional chargers
- **Time period**: 2020-2024, with high-resolution contingency event data
- **Format**: Time series data, CSV
- **License**: Open Access (CC BY 4.0)
- **Key feature**: First documented V2G contingency frequency response in real-world grid
- **Publication**: Nature npj Sustainable Mobility and Transport (2024)

### TU Dortmund High-Resolution EV Charging Dataset
- **Source**: TU Dortmund University Smart Grid Technology Lab, Zenodo
- **DOI**: 10.5281/zenodo.14065331
- **Dataset URL**: https://zenodo.org/record/14065331
- **Content**: 142 EV charging profiles with bidirectional capability
- **Profile breakdown**: 28 static V1G, 11 static V2G, 69 dynamic V1G, 34 dynamic V2G
- **Measurements**:
  - State of Charge (SoC) data
  - Power flow (kW, bidirectional for V2G profiles)
  - Grid voltage, current, power
  - Harmonics (THD) and reactive power
  - Charging efficiency
  - Sub-second temporal resolution
- **Vehicles**: 8 commercially available EVs (3 bidirectional capable)
- **Format**: CSV with synchronized measurements
- **License**: Open Access
- **Key feature**: Laboratory-controlled environment with harmonics analysis
- **Publication**: Nature Scientific Data (2025)

### Electric Nation V2G (UK): Large-scale residential V2G
- **Source**: National Grid Electricity Distribution, CrowdCharge
- **Dataset URL**: https://connecteddata.nationalgrid.co.uk/dataset/electric-nation
- **Content**: 100 Nissan EV owners with V2G chargers
- **Measurements**:
  - Import and export energy flows (bidirectional)
  - Charging behavior patterns
  - Multiple energy supplier tariffs
  - 2+ million hours of charging data
- **Scale**: 100 V2G chargers across UK (Midlands, South West England, South Wales)
- **Time period**: 2020-2022
- **Format**: CSV files, downloadable via portal
- **License**: Public data portal, open access
- **Key feature**: Multi-tariff, multi-supplier real-world V2G trial
- **Temporal resolution**: 3-15 minute intervals

### Parker Project (Denmark): Multi-brand V2G testing
- **Source**: DTU (Technical University of Denmark)
- **Report URL**: DTU Research Database (orbit.dtu.dk)
- **Content**: 50 EVs providing frequency containment reserve
- **Measurements**:
  - Frequency containment reserve (FCR) data
  - Cross-brand V2G performance (Nissan, Mitsubishi, PSA)
  - Voltage support data
  - Grid service stacking demonstrations
- **Scale**: 50 vehicles in lab tests, 10 vehicles in commercial operation
- **Time period**: August 2016 - September 2018
- **Format**: Final report and publications
- **Access**: Final Report available at DTU Research Database
- **Real results**: 100 hours V2G per vehicle, 130,000 kWh returned to grid, €1,860 revenue per vehicle per year
- **Publication**: "The Parker Project: Final Report" (Andersen et al., 2019)

### University of Delaware / PJM Frequency Regulation
- **Source**: University of Delaware V2G research group
- **Dataset URL**: www1.udel.edu/V2G/Reports.html
- **Content**: First V2G system selling electricity to wholesale power market (2013)
- **Measurements**:
  - Frequency regulation service data
  - 4-second dispatch intervals
  - Battery throughput measurements
  - Wireless communication performance
- **Scale**: Fleet providing 1MW regulation services
- **Time period**: 2007-2014+
- **Access**: Published reports and research papers
- **Key publication**: "A Test of Vehicle-to-Grid (V2G) for Energy Storage and Frequency Regulation in the PJM System" (Kempton et al., 2008)

### Additional research datasets

**UrbanEV Dataset**: 24,798 charging piles, 6 months, hourly data from Shenzhen, China. Repository: https://github.com/IntelligentSystemsLab/UrbanEV

**ST-EVCDP**: 18,061 charging piles, 30 days, 5-minute intervals. Repository: https://github.com/IntelligentSystemsLab/ST-EVCDP

**Kaggle EV Charging datasets**: Multiple datasets including fleet behavior (85 drivers), charging patterns, grid optimization, and load profiles. Search: https://www.kaggle.com/search?q=ev+charging

## OCPP testing services accelerate certification workflows

For protocol conformance testing before production deployment, **OCTT (OCPP Compliance Test Tool)** from the Open Charge Alliance provides the official certification platform with **a 14-day free trial** covering 26 testcases for OCPP 1.6 and 33 for OCPP 2.0.1. The cloud-based platform performs comprehensive protocol validation with detailed execution logs, testing both Charging Stations and CSMS implementations. Full access ranges from €6,000-€15,000 one-time plus €1,800-€2,400 annually, with member discounts available.

**Current.eco** offers an OCPP sandbox environment at €750/month (12-month minimum) for unlimited testing before certification, providing operational data access for all connected chargers. For developers wanting to avoid subscription costs, **SteVe** provides a free open-source alternative (GPL license, GitHub: https://github.com/steve-community/steve) supporting OCPP 1.2-2.0, though it requires self-hosting with Java and MySQL.

### OCTT (Official certification tool)
- **URL**: https://openchargealliance.org/test-tool/
- **Free trial**: 14 days (limited testcases: 26 for 1.6, 33 for 2.0.1)
- **Full pricing**: €6,000-€15,000 one-time + €1,800-€2,400/year
- **OCPP support**: 1.6 (422 testcases), 2.0.1 (254 testcases)
- **Features**: Cloud-based, real-time detailed logs, tests both CS and CSMS
- **Required for**: OCA certification

### Current.eco OCPP Sandbox
- **URL**: https://www.current.eco/professional
- **Pricing**: €750/month (12-month minimum)
- **Features**: Unlimited testing, operational insights, integration testing before certification
- **OCPP support**: 1.6, 2.0.1

### SteVe (Open source)
- **URL**: https://github.com/steve-community/steve
- **Pricing**: Free (GPL license)
- **OCPP support**: 1.2, 1.5, 1.6, 2.0 (in progress)
- **Features**: Web-based management, SOAP and WebSocket/JSON, MySQL backend
- **Setup**: Self-hosted Java application

## Commercial APIs enable rapid integration testing

Several OCPP platforms offer developer-friendly APIs with sandbox environments, though most require subscriptions. **Enode API** stands out with a **free Starter plan** supporting 1,000+ energy devices including 7 home charger brands and 45+ EV brands, providing normalized data across manufacturers via a single REST API. The OAuth 2.0-based authentication and comprehensive documentation at https://developers.enode.com/ make it excellent for multi-device energy management integrations.

For OCPP-specific development, **eDRV** provides a **virtual charger simulator** eliminating physical hardware requirements, allowing developers to simulate faults, RFID scans, and power consumption patterns through a REST API. **ChargeLab's Developer Program** (application required) offers early access to their unified API for all OCPP-compliant chargers with simulated endpoints for testing. **AMPECO** provides a comprehensive REST API free for existing clients with no rate limits, featuring weekly documentation updates and full platform functionality access.

### Enode API
- **URL**: https://developers.enode.com/
- **Pricing**: **Free Starter plan** + custom enterprise plans
- **Device support**: 1,000+ devices (EVs, chargers, solar, batteries)
- **Charger support**: 7 home charger brands, 17 models
- **EV support**: 45+ brands, 365 models
- **Features**: Single API for multiple energy devices, normalized data, smart charging algorithms, V2G/V2H support
- **Authentication**: OAuth 2.0 via Link UI
- **Data available**: Real-time EV data, charging status, SoC, charge rate, start/stop commands
- **Response format**: REST API, JSON
- **SDKs**: iOS, Android, Web Link UI

### eDRV API
- **URL**: https://www.edrv.io/charging-management-api
- **Key feature**: **Virtual charger simulator** (no physical hardware needed)
- **Features**: Simulate faults, RFID scans, power consumption, session autopilot, load management
- **Protocol support**: OCPP, OCPI, OpenADR normalization
- **Response format**: REST API, JSON
- **Best for**: Development and testing without physical equipment

### ChargeLab API
- **URL**: https://chargelab.co/developer-program
- **Access**: Developer Program (apply for access)
- **Features**: Single unified API for all OCPP chargers, simulated charger endpoints, custom load balancing, DER integration
- **Response format**: REST API, JSON
- **Best for**: Third-party integration

### AMPECO API
- **URL**: https://developers.ampeco.com/
- **Pricing**: **Free for existing clients** (no rate limits)
- **OCPP support**: 1.5 SOAP, 1.6 JSON/SOAP, 2.0.1 JSON
- **Features**: Comprehensive REST API, weekly documentation updates, integration with billing/CRM/ERP
- **Data available**: Charge points, EVSE, users, sessions, real-time and historical data
- **Authentication**: API keys via client account

### Switch-EV API
- **URL**: https://docs.switch-ev.com/
- **OCPP support**: 1.6J and 2.0.1 (dual OCA certified)
- **Features**: Full OCPP 2.0.1 Core & Advanced Security, ISO 15118 Plug & Charge, Josev embedded software (Apache 2.0)
- **Data available**: Real-time charger status, logs, tariffs, networks
- **Response format**: REST API, JSON

## V2G-specific platforms support bidirectional optimization

For platforms specifically focused on Vehicle-to-Grid optimization, **Switch-EV** provides the only OCPP 2.0.1 Core & Advanced Security certified implementation with full ISO 15118 Plug & Charge support. Their partnership with Nuvve integrates V2G energy aggregation with charging management, supporting ISO 15118-20 (OCPP 2.1) bidirectional charging. **gridX XENON Platform** offers V2G/V2H capabilities with ISO 15118 protocol support and APIs for pool-level flexibility management, integrating directly with grid operators for real-time data and control.

For hands-on learning about V2G deployments, the **V2G Hub global database** (www.v2g-hub.com) catalogs 150+ V2G projects worldwide with filterable project details, timelines, participants, and services. Projects range from small pilots (2 vehicles) to large-scale deployments (500 vehicles in Utrecht), providing valuable context for platform development and testing strategies.

## Recommended testing strategy for V2G platform development

Based on the available resources, implement this phased approach to efficiently test your V2G platform:

**Phase 1: OCPP protocol validation (Week 1)**
Deploy CitrineOS and EVerest in Docker containers to establish a complete OCPP 2.0.1 testing environment. Connect your platform to CitrineOS's WebSocket endpoint (port 8081) and subscribe to the Data API to capture all message types. Use EVerest's simulator to generate realistic charge point behavior and extract logs from `/tmp/everest_ocpp_logs/` for message analysis. This provides immediate access to BootNotification, StatusNotification, TransactionEvent, and MeterValues messages in production-quality format.

**Phase 2: Algorithm validation with real data (Week 2-3)**
Download the REVS dataset from Zenodo (DOI: 10.5281/zenodo.11195210) for high-resolution grid contingency data and the TU Dortmund dataset (DOI: 10.5281/zenodo.14065331) for controlled V2G profiles. Use these datasets to validate your optimization algorithms against real bidirectional power flows, SoC progression, and grid frequency response. The REVS data's 25ms resolution captures fast frequency response while TU Dortmund's sub-second measurements enable harmonics analysis and efficiency calculations.

**Phase 3: Scale testing (Week 4)**
Leverage the MobileHouse Python OCPP library to programmatically spawn 100-10,000 simulated charge points, each running in separate async tasks. Configure varied charging profiles, SoC levels, and availability patterns to stress-test your platform's concurrent connection handling and optimization algorithm performance. Monitor your system's ability to process 30-second telemetry updates across the fleet while maintaining real-time optimization decisions.

**Phase 4: Certification preparation (Week 5+)**
Use the OCTT 14-day free trial to validate protocol conformance before investing in full certification. Focus on the 33 OCPP 2.0.1 testcases in the trial to identify any protocol deviations early. If additional testing is needed, deploy SteVe as a free self-hosted alternative for ongoing conformance checks during development.

## Additional resources and access information

**Open Charge Alliance**: Membership (free tier available) provides access to OCPP specifications, JSON schemas, and test case documentation at https://openchargealliance.org/

**NREL Data Catalog**: Transportation and EV integration datasets available at https://data.nrel.gov including the "2030 National Charging Network" dataset (DOI: 10.7799/1969130)

**V2G Hub database**: Comprehensive catalog of 150+ global V2G projects at www.v2g-hub.com with filterable project details

**GitHub topic search**: Search "OCPP 2.0" and "OCPP 2.1" on GitHub for emerging implementations and community projects

**CHAdeMO Association**: V2G/V2H specifications and certification documentation for the most mature bidirectional charging ecosystem at www.chademo.com

## Critical specifications for your use case

Your platform's requirements align well with available resources:

**OCPP 2.1 WebSocket messages**: CitrineOS and EVerest provide production-ready implementations. EVerest's OCPP 2.1 support is expected Q1 2025. OpenChargingCloud WWCP and typed-ocpp already support OCPP 2.1.

**30-second telemetry sampling**: The REVS dataset provides 30-second charger resolution matching your requirements, while TU Dortmund offers sub-second resolution for more granular analysis.

**Concurrent connections (100-10,000)**: CitrineOS's microservices architecture and PostgreSQL backend support production-scale deployments. Use the MobileHouse Python library for programmatic simulator spawning at scale.

**Bidirectional power flow**: All three primary datasets (REVS, TU Dortmund, Electric Nation) include true V2G data with positive/negative power values for charge/discharge cycles.

All major resources identified are immediately accessible with either open-source licenses (Apache 2.0, MIT) or open-access data licenses (CC BY 4.0), enabling rapid deployment without licensing barriers or approval delays. Docker-based deployments reduce setup time to under 15 minutes for most components.