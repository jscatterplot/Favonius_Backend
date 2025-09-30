# Supabase Integration for EV Charging Platform

This document describes the Supabase integration implementation for the EV charging platform, providing user-facing features, authentication, and real-time data synchronization.

## Overview

The Supabase integration provides:
- **Authentication & Authorization**: JWT-based auth with role-based access control
- **User Management**: Organizations, users, and permissions
- **Fleet Management**: Vehicles, charging stations, and sites
- **Real-time Data**: Live vehicle states and charging sessions
- **Analytics**: Energy usage, cost savings, and performance metrics
- **REST API**: User-facing endpoints for web and mobile applications

## Architecture

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   WebSocket     │    │   Supabase      │    │   TimescaleDB   │
│   Handler       │◄──►│   PostgreSQL    │◄──►│   (Telemetry)   │
│   (OCPP 2.1)    │    │   (User Data)   │    │                 │
└─────────────────┘    └─────────────────┘    └─────────────────┘
         │                       │                       │
         │                       │                       │
         ▼                       ▼                       ▼
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   Redis Cache   │    │   REST API      │    │   Data Sync     │
│   (Real-time)   │    │   (Port 8080)   │    │   Service       │
└─────────────────┘    └─────────────────┘    └─────────────────┘
```

## Database Schema

### Core Tables

#### Organizations
- **organizations**: Fleet operators, site owners, utilities
- **user_organizations**: User-organization relationships with roles
- **sites**: Physical locations with charging infrastructure

#### Fleet Management
- **vehicles**: EV fleet with V2G capabilities
- **charging_stations**: OCPP-compliant charging equipment
- **charging_sessions_summary**: Completed charging sessions
- **charging_sessions_active**: Real-time active sessions

#### Scheduling & Optimization
- **charging_schedules_config**: Charging strategy configurations
- **vehicle_schedules**: Individual vehicle schedules
- **vehicle_realtime_state**: Live vehicle telemetry

### Views & Functions
- **daily_energy_summary**: Aggregated daily energy metrics
- **fleet_overview**: Organization fleet statistics
- **calculate_savings()**: Cost savings calculation function

## Configuration

### Environment Variables

```bash
# Supabase Configuration
SUPABASE_URL=https://yourproject.supabase.co
SUPABASE_ANON_KEY=your-anon-key
SUPABASE_SERVICE_KEY=your-service-key

# Database Connection (from your connection string)
SUPABASE_DB_HOST=aws-1-us-east-2.pooler.supabase.com
SUPABASE_DB_PORT=6543
SUPABASE_DB_NAME=postgres
SUPABASE_DB_USER=postgres.evdehwjbbgdgiwdvjqfk
SUPABASE_DB_PASSWORD=1NDLK5slwkI8Ka7b

# Optional Settings
SUPABASE_MAX_CONNECTIONS=20
SUPABASE_CONNECTION_TIMEOUT=30
SUPABASE_ENABLE_REALTIME=true
```

## Installation & Setup

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Initialize Database Schema

```bash
python init_database.py
```

This will create all necessary tables, indexes, functions, and RLS policies.

### 3. Start the Application

```bash
python -m src.websocket_handler.main
```

The application will start:
- **WebSocket Server**: Port 9000 (OCPP 2.1)
- **REST API Server**: Port 8080 (User-facing)
- **Health Check**: Port 8081
- **Metrics**: Port 8080/metrics

## API Endpoints

### Authentication
- `POST /auth/login` - User login
- `POST /auth/refresh` - Refresh JWT token
- `GET /auth/me` - Get current user info

### Organizations
- `GET /organizations` - List user's organizations
- `GET /organizations/{id}` - Get organization details
- `PUT /organizations/{id}` - Update organization

### Vehicles
- `GET /vehicles` - List organization vehicles
- `POST /vehicles` - Create new vehicle
- `GET /vehicles/{id}` - Get vehicle details
- `PUT /vehicles/{id}` - Update vehicle
- `DELETE /vehicles/{id}` - Delete vehicle

### Charging Stations
- `GET /stations` - List charging stations
- `POST /stations` - Create new station
- `GET /stations/{id}` - Get station details
- `PUT /stations/{id}` - Update station

### Charging Sessions
- `GET /sessions` - List charging sessions
- `GET /sessions/active` - Get active sessions
- `POST /sessions/{id}/stop` - Stop charging session

### Schedules
- `GET /schedules` - List charging schedules
- `POST /schedules` - Create schedule
- `PUT /schedules/{id}` - Update schedule
- `DELETE /schedules/{id}` - Delete schedule

### Analytics
- `GET /analytics/energy` - Energy usage analytics
- `GET /analytics/costs` - Cost analytics
- `GET /analytics/savings` - Savings calculations

### Real-time
- `GET /realtime/subscribe` - Subscribe to real-time updates

### Admin
- `GET /admin/sync-status` - Data sync status
- `POST /admin/sync` - Force data synchronization

## Authentication & Authorization

### JWT Tokens
- **Access Token**: 15-minute expiration
- **Refresh Token**: 30-day expiration
- **API Keys**: Long-lived service tokens

### Roles & Permissions
- **owner**: Full access to organization
- **admin**: Manage vehicles, stations, schedules
- **operator**: View and operate charging sessions
- **viewer**: Read-only access to analytics

### Rate Limiting
- **Read Operations**: 1000 requests/minute
- **Write Operations**: 100 requests/minute
- **Per Organization**: Separate rate limits

## Real-time Features

### WebSocket Subscriptions
- **Vehicle Updates**: Real-time vehicle state changes
- **Fleet Updates**: Organization-wide vehicle updates
- **Session Updates**: Active charging session changes
- **Optimization Updates**: Real-time optimization decisions

### Data Synchronization
- **Automatic Sync**: Every 5 minutes from TimescaleDB
- **Real-time Sync**: Active sessions and vehicle states
- **Batch Processing**: Historical data and analytics

## Security Features

### Row Level Security (RLS)
- Users can only access their organization's data
- Strict boundaries between organizations
- Role-based access control at database level

### API Security
- JWT token validation
- Rate limiting per organization
- Input validation and sanitization
- CORS configuration for web clients

### Data Protection
- Encrypted connections (TLS 1.3)
- Secure password hashing (bcrypt)
- Audit logging for compliance
- GDPR/CCPA compliance features

## Monitoring & Observability

### Health Checks
- Database connectivity
- Redis cache status
- Kafka producer health
- Supabase API status

### Metrics
- API request rates and latencies
- Database query performance
- Real-time subscription counts
- Error rates and types

### Logging
- Structured logging with correlation IDs
- Security event logging
- Performance monitoring
- Error tracking and alerting

## Development & Testing

### Local Development
```bash
# Set environment variables
export SUPABASE_URL="your-url"
export SUPABASE_SERVICE_KEY="your-key"
# ... other variables

# Run with development settings
export ENVIRONMENT=development
python -m src.websocket_handler.main
```

### Testing
```bash
# Run tests
pytest tests/

# Test specific components
pytest tests/test_supabase_client.py
pytest tests/test_auth_manager.py
pytest tests/test_api_server.py
```

### Database Migrations
```bash
# Create new migration
python -m src.websocket_handler.database_schema --create-migration "add_new_table"

# Apply migrations
python -m src.websocket_handler.database_schema --migrate
```

## Production Deployment

### Environment Setup
```bash
# Production environment variables
export ENVIRONMENT=production
export DEBUG=false
export LOG_LEVEL=INFO

# Supabase production settings
export SUPABASE_MAX_CONNECTIONS=50
export SUPABASE_CONNECTION_TIMEOUT=60
```

### Docker Deployment
```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src/ ./src/
COPY init_database.py .

CMD ["python", "-m", "src.websocket_handler.main"]
```

### Kubernetes Deployment
```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ev-charging-platform
spec:
  replicas: 3
  selector:
    matchLabels:
      app: ev-charging-platform
  template:
    metadata:
      labels:
        app: ev-charging-platform
    spec:
      containers:
      - name: websocket-handler
        image: ev-charging-platform:latest
        ports:
        - containerPort: 9000
        - containerPort: 8080
        env:
        - name: SUPABASE_URL
          valueFrom:
            secretKeyRef:
              name: supabase-secrets
              key: url
        # ... other environment variables
```

## Troubleshooting

### Common Issues

#### Database Connection Errors
```bash
# Check connection string
psql "postgresql://user:password@host:port/dbname"

# Verify Supabase credentials
curl -H "Authorization: Bearer YOUR_SERVICE_KEY" \
     https://yourproject.supabase.co/rest/v1/organizations
```

#### Authentication Issues
```bash
# Test JWT token
python -c "
import jwt
token = 'your-jwt-token'
payload = jwt.decode(token, 'your-secret', algorithms=['HS256'])
print(payload)
"
```

#### Real-time Subscription Issues
```bash
# Check Supabase real-time status
curl -H "Authorization: Bearer YOUR_SERVICE_KEY" \
     https://yourproject.supabase.co/rest/v1/realtime/status
```

### Performance Optimization

#### Database Optimization
- Use connection pooling
- Optimize queries with proper indexes
- Monitor slow query logs
- Use read replicas for analytics

#### Caching Strategy
- Cache user sessions in Redis
- Cache organization data
- Use CDN for static assets
- Implement query result caching

#### API Optimization
- Use pagination for large datasets
- Implement response compression
- Use async/await patterns
- Monitor API response times

## Support & Documentation

### API Documentation
- OpenAPI 3.0 specification available at `/docs`
- Interactive API explorer
- Request/response examples
- Authentication guide

### Additional Resources
- [Supabase Documentation](https://supabase.com/docs)
- [OCPP 2.1 Specification](https://www.openchargealliance.org/protocols/ocpp-201/)
- [PostgreSQL Documentation](https://www.postgresql.org/docs/)
- [Redis Documentation](https://redis.io/docs/)

### Contact
For technical support or questions about the Supabase integration, please contact the development team or create an issue in the project repository.
