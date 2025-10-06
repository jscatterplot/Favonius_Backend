# EV Charging Platform - WebSocket Handler

A streamlined OCPP 2.1 WebSocket handler for Vehicle-to-Grid (V2G) electric vehicle charging pilot. This service manages bidirectional communication with EV chargers, supporting up to 100 concurrent connections with sub-second response times.

## Features

### Core Functionality
- **OCPP 2.1 Protocol Support**: Full implementation of OCPP 2.1 messages including V2X operations
- **Pilot-Scale Performance**: Optimized for up to 100 concurrent WebSocket connections using uvloop
- **Bidirectional Charging**: V2X controller supporting multiple operation modes
- **Direct Persistence**: Writes charger telemetry and state directly to TimescaleDB
- **Supabase Integration**: REST API with authentication, user management, and analytics
- **Price Feeder**: CAISO OASIS data ingestion with 24-hour lookahead storage
- **Optimization Engine**: Rolling horizon schedules respecting SOC targets and energy prices
- **Comprehensive Monitoring**: Prometheus metrics and structured logging

### V2X Operation Modes
- **CentralSetpoint**: Direct power control from cloud optimization
- **LocalFrequency**: Autonomous frequency response
- **LocalLoadBalancing**: Building-level optimization
- **ExternalSetpoint**: Third-party EMS integration

### Security & Reliability
- TLS 1.3 support with client certificate validation
- Rate limiting and connection management
- Circuit breaker patterns for fault tolerance
- Graceful degradation and automatic failover
- Comprehensive health checks and monitoring

## Architecture

```
┌─────────────────┐    ┌──────────────────┐    ┌──────────────────┐
│   EV Chargers   │◄──►│  WebSocket       │    │   Supabase       │
│   (OCPP 2.1)    │    │  Handler         │    │  (User DB & API) │
└─────────────────┘    └────────▲─────────┘    └────────▲────────┘
                                 │                           │
                                 ▼                           │
                         ┌──────────────────┐                │
                         │  TimescaleDB     │◄──────────────┘
                         │ (Telemetry &     │
                         │  Analytics)      │
                         └──────────────────┘
```

## Quick Start

### Docker Compose (Development)

1. **Clone and setup environment**:
```bash
git clone <repository>
cd websocket-handler
cp .env.example .env  # Edit with your configuration
```

2. **Start services**:
```bash
docker-compose up -d
```

3. **Verify deployment**:
```bash
# Check WebSocket server
curl http://localhost:8081/health

# Check metrics
curl http://localhost:8080/metrics

# View logs
docker-compose logs -f websocket-handler
```

### Kubernetes (Production)

> Production manifests are maintained but currently include legacy Redis/Kafka references. Update them to match the simplified architecture before deployment.

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `WEBSOCKET_PORT` | WebSocket server port | 9000 |
| `MAX_CONNECTIONS` | Maximum concurrent connections | 100 |
| `HEARTBEAT_INTERVAL` | Heartbeat interval (seconds) | 30 |
| `LOG_LEVEL` | Logging level | INFO |
| `ENVIRONMENT` | Environment (dev/staging/prod) | development |

### TLS Configuration (Optional)

```bash
# Generate self-signed certificates for testing
openssl req -x509 -newkey rsa:4096 -keyout server.key -out server.crt -days 365 -nodes

# Set environment variables
export TLS_CERT_PATH=/path/to/server.crt
export TLS_KEY_PATH=/path/to/server.key
export TLS_VERIFY_CLIENT=false
```

## OCPP 2.1 Message Support

### Supported Messages
- **BootNotification**: Charger registration and capabilities
- **StatusNotification**: Connector status updates
- **TransactionEvent**: Charging session lifecycle
- **MeterValues**: Real-time power and energy measurements
- **NotifyEVChargingNeeds**: Vehicle energy requirements
- **NotifyEVChargingSchedule**: EV proposed charging schedule
- **Heartbeat**: Connection keep-alive
- **Authorize**: User authentication
- **DataTransfer**: Custom vendor messages

### Supported Commands
- **SetChargingProfile**: V2G power setpoint control
- **GetCompositeSchedule**: Current power schedule query
- **RemoteStartTransaction**: Remote charging initiation
- **RemoteStopTransaction**: Remote charging termination

## API Endpoints

### Health Checks
- `GET /health` - Comprehensive health check
- `GET /liveness` - Kubernetes liveness probe
- `GET /readiness` - Kubernetes readiness probe
- `GET /status` - Detailed status information

### Metrics
- `GET /metrics` - Prometheus metrics endpoint
- `GET /metrics/summary` - Lightweight metrics summary

## Monitoring & Observability

### Prometheus Metrics
- `websocket_connections_active` - Active connection count
- `websocket_messages_received_total` - Messages received by type
- `websocket_message_processing_seconds` - Processing latency
- `timescaledb_query_duration_seconds` - Timescale query latency (see monitoring docs)

### Structured Logging
All logs are output in JSON format with correlation IDs for distributed tracing:

```json
{
  "timestamp": "2024-01-15T10:30:00Z",
  "level": "INFO",
  "logger": "websocket_handler.server",
  "message": "Connection established",
  "station_id": "CS-001",
  "connection_id": "uuid-123",
  "client_ip": "192.168.1.100"
}
```

## Development

### Local Development Setup

1. **Install dependencies**:
```bash
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows
pip install -r requirements.txt
```

2. **Start Redis and Kafka locally**:
```bash
docker-compose up -d redis kafka
```

3. **Run the application**:
```bash
export REDIS_URL=redis://localhost:6379
export KAFKA_BROKERS=localhost:9092
python -m src.websocket_handler.main
```

### Testing

```bash
# Run tests
pytest tests/

# Run with coverage
pytest --cov=src tests/

# Load testing
python tests/load_test.py --connections 1000 --duration 300
```

### Code Quality

```bash
# Format code
black src/ tests/
isort src/ tests/

# Type checking
mypy src/

# Linting
flake8 src/ tests/
```

## Performance Tuning

### System-Level Optimizations
- Use uvloop for better async performance
- Tune TCP parameters for high connection counts
- Configure proper file descriptor limits
- Use connection pooling for database connections

### WebSocket Optimizations
- Disable compression for better CPU utilization
- Use binary message formats where possible
- Implement proper backpressure handling
- Monitor memory usage per connection

### Redis Optimizations
- **Enhanced Redis Client** with cluster support and atomic operations
- **Time-series telemetry storage** with automatic retention
- **Fleet management bitmaps** for O(1) availability queries
- **Market data caching** with price forecasting
- **Lua scripts** for atomic state management
- **Redis Streams** for grid signals and event handling
- **Connection pooling** and compression for optimal performance

For detailed Redis implementation, see [REDIS_IMPLEMENTATION.md](REDIS_IMPLEMENTATION.md)

## Deployment Considerations

### Resource Requirements
- **CPU**: 2-4 cores per instance
- **Memory**: 4GB per instance (1GB per 2500 connections)
- **Network**: 100Mbps per instance
- **Storage**: 10GB for logs and temporary data

### Scaling Guidelines
- Scale horizontally based on connection count
- Use session affinity for WebSocket connections
- Monitor queue depths and processing latency
- Consider connection draining for deployments

### Security Recommendations
- Enable TLS in production environments
- Use client certificate authentication
- Implement IP whitelisting where applicable
- Regular security updates and vulnerability scanning
- Secure secrets management (Kubernetes secrets, HashiCorp Vault)

## Troubleshooting

### Common Issues

**High Memory Usage**:
- Check connection count and message queue depths
- Monitor for memory leaks in long-running connections
- Verify proper message acknowledgments

**Connection Drops**:
- Check network connectivity and firewall settings
- Verify heartbeat configuration
- Monitor load balancer health checks

**Slow Message Processing**:
- Check Redis and Kafka latencies
- Monitor CPU and memory utilization
- Verify optimization engine performance

### Debug Mode

```bash
export DEBUG=true
export LOG_LEVEL=DEBUG
python -m src.websocket_handler.main
```

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Make your changes and add tests
4. Commit your changes (`git commit -m 'Add amazing feature'`)
5. Push to the branch (`git push origin feature/amazing-feature`)
6. Open a Pull Request

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Support

For support and questions:
- Create an issue in the repository
- Contact the development team
- Check the documentation and troubleshooting guides

---

**Built for the future of electric mobility and grid integration.**
