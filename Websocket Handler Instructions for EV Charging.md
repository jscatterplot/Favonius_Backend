# WebSocket Handler for OCPP 2.1 Charger Communication

## System Architecture Overview
The WebSocket handler manages real-time bidirectional communication with EV chargers using OCPP 2.1 protocol. This system must handle 10,000+ concurrent connections with 30-second response cycles for grid optimization.

## Core Requirements

### Connection Management
- Implement WebSocket server on port 9000 using `websockets` library in Python
- Support OCPP 2.1 subprotocol negotiation: `ocpp2.1` must be accepted
- Maintain persistent connections with automatic reconnection logic
- Implement exponential backoff for reconnection attempts (2s, 4s, 8s, 16s, max 60s)
- Connection pool management using asyncio with configurable pool size (default: 10,000)
- Heartbeat interval: 30 seconds with 10-second timeout tolerance

### OCPP 2.1 Message Handling
Build message handlers for these critical V2G operations:

#### Required Message Types
- `BootNotification`: Register charger on connection
- `StatusNotification`: Track EVSE/Connector availability states
- `TransactionEvent`: Monitor charging session lifecycle
- `MeterValues`: Collect energy flow data (positive for charging, negative for discharging)
- `SetChargingProfile`: Send V2G schedules with power setpoints
- `NotifyEVChargingNeeds`: Receive vehicle energy requirements
- `NotifyEVChargingSchedule`: Get EV's planned power profile
- `GetCompositeSchedule`: Query current power limits/setpoints
- `PullDynamicScheduleUpdate`: Real-time power adjustments

#### Message Format
All messages follow JSON-RPC 2.0 format:
```
[MessageTypeId, UniqueId, Action, Payload]
```
Where MessageTypeId: 2=CALL, 3=CALLRESULT, 4=CALLERROR

### V2X Controller Integration
Access these variables per EVSE for bidirectional control:
- `V2XChargingCtrlr.Enabled`: Enable/disable V2X functionality
- `V2XChargingCtrlr.SupportedOperationModes`: Available grid service modes
- `V2XChargingCtrlr.TxUpdatedInterval[OperationMode]`: Data collection frequency

Operation modes to implement:
- `CentralSetpoint`: Direct power control from cloud
- `LocalFrequency`: Autonomous frequency response
- `LocalLoadBalancing`: Building-level optimization
- `ExternalSetpoint`: External EMS integration

### Data Flow to Redis
For each connected charger, maintain real-time state in Redis:
- Connection status and last heartbeat timestamp
- Current charging state (Idle, Charging, Discharging, SuspendedEVSE, SuspendedEV)
- Power flow metrics (kW, positive/negative)
- State of Charge (SoC) percentage
- Maximum charge/discharge power limits
- Active charging profile ID and parameters
- Grid service mode active

Redis key patterns:
```
charger:{station_id}:connection -> connection metadata
charger:{station_id}:state -> current operational state
charger:{station_id}:metrics -> real-time power metrics
charger:{station_id}:profile -> active charging profile
```

### Connection Registry
Implement distributed connection registry using Redis:
- Track which server instance handles each connection
- Support horizontal scaling across multiple WebSocket servers
- Implement connection migration for server maintenance
- Use Redis pub/sub for cross-server communication

### Performance Optimization
- Use uvloop for enhanced asyncio performance
- Implement connection pooling with asyncio.Queue
- Batch Redis operations using pipelines
- Message compression using zlib for large payloads
- Implement circuit breaker pattern for failing connections
- Memory-efficient message buffering (max 1000 messages per connection)

### Error Handling and Resilience
- Graceful degradation when Redis unavailable (local cache fallback)
- Message queue persistence during connection interruptions
- Automatic profile rollback on execution failures
- Dead letter queue for unprocessable messages
- Comprehensive logging with correlation IDs for message tracing

### Monitoring and Metrics
Track and expose via Prometheus:
- Active WebSocket connections count
- Message processing latency (p50, p95, p99)
- Message queue depth per connection
- Redis operation latency
- Connection churn rate
- Error rates by message type

### Security Requirements
- TLS 1.3 for all WebSocket connections
- Client certificate validation for charger authentication
- Rate limiting per connection (100 messages/minute)
- Message size limits (64KB default, configurable)
- Connection timeout after 5 minutes of inactivity
- IP-based access control lists

### Horizontal Scaling Strategy
- Use Redis Streams for work distribution
- Implement consistent hashing for connection assignment
- Session affinity through load balancer
- Graceful shutdown with connection draining
- Zero-downtime deployment support

### Integration Points
- Publish charging events to Kafka topic `charger.events`
- Subscribe to optimization decisions from `optimization.commands`
- Forward telemetry to TimescaleDB writer service
- Emit alerts to monitoring service for critical states

## Implementation Notes

### Library Dependencies
- `websockets>=12.0` for WebSocket server
- `ocpp>=0.23.0` for OCPP message handling  
- `redis>=5.0` for state management
- `aiokafka>=0.10` for event streaming
- `prometheus-client>=0.19` for metrics
- `structlog>=24.0` for structured logging

### Configuration Parameters
Define environment variables:
- `WEBSOCKET_PORT`: Server port (default: 9000)
- `REDIS_URL`: Redis connection string
- `KAFKA_BROKERS`: Comma-separated broker list
- `MAX_CONNECTIONS`: Maximum concurrent connections
- `HEARTBEAT_INTERVAL`: Seconds between heartbeats
- `MESSAGE_TIMEOUT`: Seconds before message timeout
- `TLS_CERT_PATH`: Server certificate path
- `TLS_KEY_PATH`: Server private key path

### Testing Requirements
- Unit tests for all message handlers
- Integration tests with OCPP simulator
- Load testing for 10,000 concurrent connections
- Chaos engineering tests for network failures
- Performance benchmarks for message processing

### Deployment Considerations
- Deploy behind load balancer with WebSocket support
- Configure health check endpoint at `/health`
- Implement readiness probe checking Redis connectivity
- Set resource limits: 4GB RAM, 2 CPU cores per instance
- Enable connection draining on shutdown signal
- Configure log aggregation to centralized system