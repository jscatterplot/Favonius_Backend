# Redis Memory Store for Real-Time V2G Operations

## Architecture Overview
Redis serves as the high-performance memory store for real-time state management, caching, and inter-service communication. The system maintains sub-millisecond response times for optimization decisions.

## Redis Cluster Configuration

### Deployment Topology
- Mode: Redis Cluster with 6 nodes (3 masters, 3 replicas)
- Memory per node: 16GB
- Persistence: AOF with rewrite on 100MB growth
- Replication: Asynchronous with 1 replica per master
- Failover: Automatic with sentinel monitoring

### Connection Settings
```yaml
redis_cluster:
  nodes:
    - host: redis-master-1.v2g.internal:6379
    - host: redis-master-2.v2g.internal:6379
    - host: redis-master-3.v2g.internal:6379
  password: ${REDIS_PASSWORD}
  ssl: true
  max_connections: 1000
  socket_keepalive: true
  socket_timeout: 5
  retry_on_timeout: true
  health_check_interval: 30
```

## Data Structures and Key Patterns

### Charger State Management

#### Connection Registry
```redis
# Hash: Active WebSocket connections
HSET charger:connections:{station_id} 
  server_id "ws-server-1"
  connected_at "2025-09-26T10:30:00Z"
  last_heartbeat "2025-09-26T10:35:30Z"
  protocol_version "ocpp2.1"
  
EXPIRE charger:connections:{station_id} 120  # 2-minute TTL
```

#### Real-Time State
```redis
# Hash: Current charger state
HSET charger:state:{station_id}
  status "Charging"
  power_kw "50.5"
  soc_percent "65.0"
  max_charge_power_kw "150.0"
  max_discharge_power_kw "100.0"
  operation_mode "CentralSetpoint"
  vehicle_connected "true"
  session_id "uuid-session-123"
  
TTL: 300 seconds (5 minutes)
```

#### Telemetry Buffer
```redis
# Time Series: Recent telemetry
TS.ADD charger:telemetry:{station_id}:power * 50.5
TS.ADD charger:telemetry:{station_id}:soc * 65.0
TS.ADD charger:telemetry:{station_id}:voltage * 400.0

# Retention: 24 hours, downsampling every 5 minutes
TS.CREATE charger:telemetry:{station_id}:power 
  RETENTION 86400000 
  DUPLICATE_POLICY LAST
  
TS.RULE charger:telemetry:{station_id}:power 
  charger:telemetry:{station_id}:power:5m 
  AGGREGATION avg 300000
```

### Fleet Optimization Data

#### Fleet State Bitmap
```redis
# Bitmap: Vehicle availability by index
SETBIT fleet:{operator_id}:available 0 1  # Vehicle 0 available
SETBIT fleet:{operator_id}:available 1 0  # Vehicle 1 unavailable
BITCOUNT fleet:{operator_id}:available    # Count available vehicles
```

#### Optimization Constraints
```redis
# Hash: Fleet-level constraints
HSET fleet:{operator_id}:constraints
  min_fleet_soc "20.0"
  max_discharge_power "500.0"
  reserved_vehicles "5"
  priority_charging_enabled "false"
  grid_service_opted_in "true"
  
TTL: No expiration (persistent configuration)
```

#### Charging Queue
```redis
# Sorted Set: Priority queue for charging
ZADD fleet:{operator_id}:queue {priority_score} {vehicle_id}

# Priority score calculation:
# score = (100 - current_soc) * urgency_weight + departure_time_unix
```

### Market Data Cache

#### Electricity Prices
```redis
# Hash: Current LMP prices by node
HSET prices:lmp:current
  TH_SP15_GEN-APND "45.67"
  TH_NP15_GEN-APND "42.30"
  updated_at "2025-09-26T10:30:00Z"
  
TTL: 300 seconds (5 minutes)

# Sorted Set: Price forecast timeline
ZADD prices:forecast:{node_id} {unix_timestamp} {price_json}
```

#### Grid Signals
```redis
# Stream: Grid control signals
XADD grid:signals:stream * 
  signal_type "frequency_regulation"
  target_mw "10.5"
  response_time_sec "30"
  compensation_rate "0.05"
  expires_at "2025-09-26T11:00:00Z"
```

### Optimization Engine Integration

#### Decision Cache
```redis
# Hash: Latest optimization decision
HSET optimization:decision:latest
  decision_id "uuid-decision-456"
  computed_at "2025-09-26T10:29:30Z"
  objective_value "1250.50"
  computation_time_ms "450"
  
# List: Decision history (capped)
LPUSH optimization:decisions {decision_json}
LTRIM optimization:decisions 0 99  # Keep last 100
```

#### Schedule Distribution
```redis
# Pub/Sub: Broadcast schedules to chargers
PUBLISH schedules:{station_id} {schedule_json}

# Hash: Active schedule per charger
HSET charger:schedule:{station_id}
  profile_id "profile-789"
  start_time "2025-09-26T10:30:00Z"
  end_time "2025-09-26T11:00:00Z"
  periods {periods_json}
```

### Session Management

#### Active Sessions
```redis
# Hash: Session details
HSET session:{session_id}
  station_id "CS-001"
  vehicle_id "VIN12345"
  start_time "2025-09-26T09:00:00Z"
  start_soc "30.0"
  energy_delivered_kwh "15.5"
  energy_received_kwh "0.0"
  
TTL: 86400 seconds (24 hours after session end)
```

#### Session Index
```redis
# Set: Active sessions per operator
SADD sessions:active:{operator_id} {session_id}

# Sorted Set: Sessions by start time
ZADD sessions:timeline {start_timestamp} {session_id}
```

## Pub/Sub Channels

### Event Broadcasting
```redis
# Channel patterns
notify:charger:{station_id}     # Charger state changes
notify:optimization:complete     # New optimization ready
notify:schedule:{station_id}     # Schedule updates
notify:grid:signal              # Grid control signals
notify:alert:{severity}         # System alerts
```

### Message Format
```json
{
  "event_type": "state_change",
  "timestamp": "2025-09-26T10:30:00Z",
  "entity_id": "CS-001",
  "old_value": "Idle",
  "new_value": "Charging",
  "metadata": {}
}
```

## Lua Scripts for Atomic Operations

### Update Charger State Atomically
```lua
-- update_charger_state.lua
local station_id = KEYS[1]
local state_json = ARGV[1]
local timestamp = ARGV[2]

-- Update state
redis.call('HSET', 'charger:state:' .. station_id, 'data', state_json)
redis.call('HSET', 'charger:state:' .. station_id, 'updated_at', timestamp)
redis.call('EXPIRE', 'charger:state:' .. station_id, 300)

-- Update heartbeat
redis.call('HSET', 'charger:connections:' .. station_id, 'last_heartbeat', timestamp)

-- Publish notification
redis.call('PUBLISH', 'notify:charger:' .. station_id, state_json)

return 'OK'
```

### Acquire Optimization Lock
```lua
-- acquire_optimization_lock.lua
local lock_key = KEYS[1]
local lock_id = ARGV[1]
local ttl = ARGV[2]

local current = redis.call('GET', lock_key)
if current == false then
    redis.call('SET', lock_key, lock_id, 'PX', ttl)
    return 1
elseif current == lock_id then
    redis.call('PEXPIRE', lock_key, ttl)
    return 1
else
    return 0
end
```

## Performance Optimization

### Memory Management
- MaxMemory Policy: `allkeys-lru`
- MaxMemory: 12GB (75% of available)
- Key expiration strategy: Lazy + periodic
- Memory sampling: 10 keys per eviction

### Pipeline Operations
```python
# Batch operations in pipeline
pipe = redis_client.pipeline()
for station_id in station_ids:
    pipe.hget(f'charger:state:{station_id}', 'power_kw')
    pipe.hget(f'charger:state:{station_id}', 'soc_percent')
results = pipe.execute()
```

### Connection Pooling
```python
pool = redis.ConnectionPool(
    max_connections=100,
    max_connections_per_node=20,
    socket_keepalive=True,
    socket_keepalive_options={
        1: 1,  # TCP_KEEPIDLE
        2: 3,  # TCP_KEEPINTVL
        3: 5   # TCP_KEEPCNT
    }
)
```

## Monitoring and Alerting

### Key Metrics
```redis
# Monitor these metrics via INFO command
- used_memory_human
- connected_clients
- total_commands_processed
- instantaneous_ops_per_sec
- keyspace_hits / (keyspace_hits + keyspace_misses)
- evicted_keys
- rejected_connections
```

### Health Checks
```python
def health_check():
    # Check cluster health
    cluster_info = redis_client.cluster_info()
    if cluster_info['cluster_state'] != 'ok':
        raise HealthCheckError('Cluster unhealthy')
    
    # Check memory usage
    info = redis_client.info('memory')
    usage_percent = info['used_memory'] / info['maxmemory']
    if usage_percent > 0.9:
        raise HealthCheckWarning('Memory usage > 90%')
    
    # Check replication lag
    for node in redis_client.cluster_nodes():
        if node['flags'] == 'slave':
            lag = node['master_link_down_since']
            if lag and lag > 10000:  # 10 seconds
                raise HealthCheckError(f'Replication lag: {lag}ms')
```

## Backup and Recovery

### Persistence Configuration
```redis
# AOF configuration
appendonly yes
appendfsync everysec
no-appendfsync-on-rewrite no
auto-aof-rewrite-percentage 100
auto-aof-rewrite-min-size 64mb

# Snapshot configuration (backup)
save 900 1      # After 900 sec if at least 1 key changed
save 300 10     # After 300 sec if at least 10 keys changed
save 60 10000   # After 60 sec if at least 10000 keys changed
```

### Backup Strategy
- Hourly AOF backups to S3
- Daily RDB snapshots to S3
- Cross-region replication for DR
- Point-in-time recovery within 1 hour

## Security Configuration

### Access Control
```redis
# ACL rules for different services
ACL SETUSER websocket_service +@read +@write +@stream ~charger:* ~session:* on >{password}
ACL SETUSER optimization_service +@all ~* on >{password}
ACL SETUSER monitoring_service +@read ~* on >{password}
```

### Network Security
- TLS 1.3 for client connections
- Mutual TLS for cluster communication
- IP whitelisting via firewall rules
- VPC isolation for production cluster