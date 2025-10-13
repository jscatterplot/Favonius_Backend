# Product Requirements Document: V2G Real-Time Optimization Platform

## Executive Summary

This document defines requirements for a Vehicle-to-Grid optimization platform enabling electric vehicle fleet operators to reduce demand charges by 30-50% through intelligent bidirectional charging. The system processes real-time telemetry, electricity prices, and grid signals to generate optimal charge/discharge schedules within 30-second decision cycles.

## Problem Statement

Commercial and industrial facilities face increasing demand charges that can represent 30-70% of their electricity bills. A single 15-minute peak can cost thousands of dollars monthly. Meanwhile, EV fleets sit idle 95% of the time, representing untapped battery capacity worth $5,000-15,000 per vehicle annually in grid services revenue.

## User Stories

### Fleet Manager
- **As a** fleet manager, **I want** real-time visibility into vehicle charge states and availability **so that** I can ensure vehicles are ready for scheduled routes
- **As a** fleet manager, **I want** automated charging schedules that respect vehicle departure times **so that** operations are never disrupted
- **As a** fleet manager, **I want** override controls for emergency situations **so that** I can prioritize critical vehicles

### Energy Manager
- **As an** energy manager, **I want** predictive alerts before demand peaks occur **so that** I can initiate load reduction measures
- **As an** energy manager, **I want** automated V2G dispatch during peak periods **so that** demand charges are minimized without manual intervention
- **As an** energy manager, **I want** detailed cost savings reports **so that** I can demonstrate ROI to management

### System Administrator
- **As a** system administrator, **I want** WebSocket connection monitoring **so that** I can ensure all chargers remain online
- **As a** system administrator, **I want** automated failover mechanisms **so that** the system remains operational during component failures
- **As a** system administrator, **I want** audit logs of all optimization decisions **so that** I can troubleshoot and comply with regulations

## Functional Requirements

### Data Ingestion Layer

#### FR1: OCPP 2+ WebSocket Handler
- **FR1.1**: Establish persistent WebSocket connections with OCPP 2+ compliant chargers
- **FR1.2**: Process BootNotification, StatusNotification, and TransactionEvent messages
- **FR1.3**: Handle MeterValues with 30-second sampling frequency
- **FR1.4**: Support 100 concurrent charger connections per server instance (simplified for initial implementation)
- **FR1.5**: Implement exponential backoff reconnection (2s, 4s, 8s, 16s, max 60s)

#### FR2: Market Data Integration
- **FR2.1**: Fetch real-time LMP prices from CAISO OASIS API every 5 minutes
- **FR2.2**: Subscribe to day-ahead market prices for 24-hour forecasting
- **FR2.3**: Process fifteen-minute market (FMM) prices for near-term optimization
- **FR2.4**: Map facility locations to nearest CAISO pricing nodes
- **FR2.5**: Cache price data in memory with 10-minute TTL (Redis removed for simplification)

#### FR3: Telemetry Processing
- **FR3.1**: Ingest power flow measurements (kW) with positive/negative differentiation
- **FR3.2**: Track State of Charge (SoC) percentage with 1% granularity
- **FR3.3**: Monitor grid frequency deviations in millihertz
- **FR3.4**: Record voltage, current, and power factor measurements
- **FR3.5**: Store telemetry in TimescaleDB hypertables with 1-day chunks

### Optimization Engine

#### FR4: Julia Optimization Solver
- **FR4.1**: Formulate mixed-integer linear program for fleet dispatch
- **FR4.2**: Minimize total cost function: energy costs - V2G revenue + demand charges
- **FR4.3**: Respect vehicle departure time constraints with required SoC
- **FR4.4**: Enforce mutual exclusion between charging and discharging states
- **FR4.5**: Complete optimization within 800ms for 100-vehicle fleet (simplified target)

#### FR5: Constraint Management
- **FR5.1**: Enforce minimum SoC threshold of 20% for battery health
- **FR5.2**: Respect maximum charge/discharge power limits per vehicle
- **FR5.3**: Maintain grid connection capacity constraints
- **FR5.4**: Honor vehicle availability windows from schedule integration
- **FR5.5**: Apply fleet-level aggregate power limits

#### FR6: Rolling Horizon Implementation
- **FR6.1**: Execute optimization every 30 seconds with 4-hour lookahead
- **FR6.2**: Apply only first time-step decisions to maintain adaptability
- **FR6.3**: Warm-start subsequent optimizations from previous solutions
- **FR6.4**: Maintain solution continuity through gradual adjustments
- **FR6.5**: Store optimization decisions with UUID identifiers

### Control Interface

#### FR7: Charging Profile Management
- **FR7.1**: Generate SetChargingProfile commands with power setpoints
- **FR7.2**: Support TxDefaultProfile for baseline charging behavior
- **FR7.3**: Implement TxProfile for transaction-specific control
- **FR7.4**: Stack charging profiles with priority levels (0-9)
- **FR7.5**: Rollback to previous profile on execution failure

#### FR8: V2G Operation Modes
- **FR8.1**: Support CentralSetpoint mode for direct power control
- **FR8.2**: Implement LocalFrequency mode for autonomous grid response
- **FR8.3**: Enable LocalLoadBalancing for building-level optimization
- **FR8.4**: Provide ExternalSetpoint integration for third-party EMS
- **FR8.5**: Transition between modes without service interruption

### State Management

#### FR9: In-Memory State Management (Redis Removed for Simplification)
- **FR9.1**: Maintain charger connection registry in application memory
- **FR9.2**: Store current vehicle states in local data structures
- **FR9.3**: Implement fleet availability tracking in memory
- **FR9.4**: Cache active charging schedules with 5-minute expiration
- **FR9.5**: Persist critical state changes directly to TimescaleDB (event bus deferred)

#### FR10: TimescaleDB Historical Storage
- **FR10.1**: Create continuous aggregates for hourly energy summaries
- **FR10.2**: Implement compression policies after 7 days
- **FR10.3**: Maintain 2-year data retention for compliance
- **FR10.4**: Support sub-second query performance on recent data
- **FR10.5**: Replicate to Supabase for user-facing analytics

## Non-Functional Requirements

### Performance Requirements

#### NFR1: Latency Specifications
- **NFR1.1**: End-to-end decision latency < 1000ms (p99)
- **NFR1.2**: In-memory operations < 5ms (p95) (Redis removed)
- **NFR1.3**: Database writes < 50ms (p95)
- **NFR1.4**: WebSocket message transmission < 150ms (p95)
- **NFR1.5**: API response time < 200ms for real-time endpoints

#### NFR2: Throughput Specifications
- **NFR2.1**: Process 1000 optimization decisions per second
- **NFR2.2**: Ingest 100,000 telemetry points per second
- **NFR2.3**: Handle 50 concurrent API requests per server
- **NFR2.4**: Support 100 WebSocket connections per instance (simplified target)
- **NFR2.5**: Execute 1000 database transactions per second

### Reliability Requirements

#### NFR3: System Availability
- **NFR3.1**: Maintain 99.9% uptime (8.76 hours downtime annually)
- **NFR3.2**: Automatic failover within 30 seconds
- **NFR3.3**: Zero data loss during planned maintenance
- **NFR3.4**: Recovery Point Objective (RPO) of 1 minute
- **NFR3.5**: Recovery Time Objective (RTO) of 5 minutes

#### NFR4: Fault Tolerance
- **NFR4.1**: Continue operation with single component failure
- **NFR4.2**: Graceful degradation during component unavailability (Redis removed)
- **NFR4.3**: Circuit breaker activation after 5 consecutive failures
- **NFR4.4**: Automatic retry with exponential backoff
- **NFR4.5**: Dead letter queue for unprocessable messages

### Security Requirements

#### NFR5: Authentication and Authorization
- **NFR5.1**: Implement JWT authentication with 15-minute token expiration
- **NFR5.2**: Use bcrypt with cost factor 12 for password hashing
- **NFR5.3**: Enforce role-based access control (owner, admin, operator, viewer)
- **NFR5.4**: Support OAuth 2.0 integration for enterprise SSO
- **NFR5.5**: Implement API key authentication for service accounts

#### NFR6: Data Protection
- **NFR6.1**: Encrypt data in transit using TLS 1.3
- **NFR6.2**: Encrypt data at rest using AES-256
- **NFR6.3**: Implement field-level encryption for PII
- **NFR6.4**: Maintain audit logs for 90 days minimum
- **NFR6.5**: Comply with GDPR and CCPA requirements

### Scalability Requirements

#### NFR7: Horizontal Scaling
- **NFR7.1**: Support Kubernetes horizontal pod autoscaling
- **NFR7.2**: Implement stateless service design
- **NFR7.3**: Use consistent hashing for connection distribution
- **NFR7.4**: Support cross-region deployment
- **NFR7.5**: Enable zero-downtime deployments

#### NFR8: Resource Efficiency
- **NFR8.1**: Memory usage < 4GB per service instance
- **NFR8.2**: CPU utilization < 70% under normal load
- **NFR8.3**: Database connection pool maximum of 100
- **NFR8.4**: Redis memory usage < 75% of allocation
- **NFR8.5**: Network bandwidth < 100Mbps per instance

## Acceptance Criteria

### System Integration
- [ ] Successfully connect to 10 different OCPP 2.1 charger models
- [ ] Process CAISO price data with < 1% error rate
- [ ] Complete end-to-end optimization cycle in < 30 seconds
- [ ] Demonstrate 30% demand charge reduction in simulation
- [ ] Maintain stable operation for 7-day continuous test

### Optimization Quality
- [ ] Achieve 95% forecast accuracy for 15-minute load prediction
- [ ] Respect 100% of hard constraints (departure times, SoC requirements)
- [ ] Generate feasible solutions for 99.9% of optimization runs
- [ ] Reduce peak demand by minimum 15% in test scenarios
- [ ] Complete optimization within 800ms for 100-vehicle fleet

### User Experience
- [ ] Display real-time vehicle status updates within 2 seconds
- [ ] Generate cost savings reports in < 5 seconds
- [ ] Support schedule override without system restart
- [ ] Provide mobile-responsive dashboard interface
- [ ] Enable CSV export of all historical data

### Operational Readiness
- [ ] Deploy using Infrastructure as Code (Terraform)
- [ ] Implement comprehensive monitoring with Prometheus/Grafana
- [ ] Configure automated backups with 1-hour RPO
- [ ] Document all API endpoints with OpenAPI 3.0
- [ ] Provide runbooks for common operational procedures

## Technical Constraints

### Infrastructure Requirements
- **Cloud Provider**: AWS or Google Cloud Platform
- **Kubernetes Version**: 1.28 or higher
- **Database Versions**: PostgreSQL 15 with TimescaleDB 2.13, Redis 7.2
- **Message Queue**: Apache Kafka 3.6 or AWS Kinesis
- **Container Runtime**: Docker 24.0 with multi-stage builds

### Development Standards
- **Code Coverage**: Minimum 80% unit test coverage
- **Documentation**: Inline comments for complex algorithms
- **API Design**: RESTful with JSON:API specification
- **Version Control**: Git with conventional commits
- **CI/CD Pipeline**: Automated testing and deployment via GitHub Actions

### Regulatory Compliance
- **Grid Codes**: IEEE 1547-2018 for distributed energy resources
- **Communication**: OCPP 2.1 certification required
- **Data Privacy**: SOC 2 Type II compliance
- **Energy Markets**: CAISO tariff compliance for wholesale participation
- **Safety Standards**: UL 1741 SA for grid-interactive inverters

## Monitoring and Observability

### Key Performance Indicators
- **Optimization Success Rate**: Target > 99.9%
- **Average Decision Latency**: Target < 500ms
- **Peak Demand Reduction**: Target > 30%
- **System Availability**: Target > 99.9%
- **Cost Savings Achieved**: Target > $1000/vehicle/year

### Alerting Thresholds
- **Critical**: Optimization failures > 1% in 5-minute window
- **Warning**: Decision latency > 800ms (p95)
- **Info**: WebSocket disconnections > 10 per minute
- **Critical**: Redis memory usage > 90%
- **Warning**: Database query time > 100ms (p95)

## Glossary

- **CAISO**: California Independent System Operator
- **LMP**: Locational Marginal Price
- **OCPP**: Open Charge Point Protocol
- **SoC**: State of Charge
- **V2G**: Vehicle-to-Grid
- **EVSE**: Electric Vehicle Supply Equipment
- **FMM**: Fifteen Minute Market
- **RTM**: Real-Time Market
- **DAM**: Day-Ahead Market
- **MIP**: Mixed-Integer Programming

## Appendices

### A. OCPP 2.1 Message Sequences
Detailed message flow diagrams for connection establishment, transaction management, and charging profile execution.

### B. Optimization Model Formulation
Complete mathematical formulation of the fleet dispatch optimization problem with objective function and constraints.

### C. Database Schema
Comprehensive TimescaleDB and Redis schema definitions with indexes and partitioning strategies.

### D. API Documentation
OpenAPI 3.0 specification for all REST endpoints with request/response examples.

### E. Deployment Architecture
System architecture diagrams showing component interactions and data flows.