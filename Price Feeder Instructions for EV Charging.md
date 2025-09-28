# California ISO Electricity Price Data Feeder

## System Overview
The price feeder service continuously fetches real-time and day-ahead electricity prices from California ISO (CAISO) OASIS API, processes the data, and distributes it to the optimization engine and other services.

## CAISO API Integration

### API Endpoints and Authentication
```python
# Base configuration
CAISO_CONFIG = {
    "base_url": "https://oasis.caiso.com/oasisapi/SingleZip",
    "timeout": 30,
    "retry_count": 3,
    "retry_delay": 5,
    "max_concurrent_requests": 5
}

# No authentication required for public OASIS data
# Rate limiting: 1000 requests per hour per IP
```

### Market Types and Query Parameters

#### Real-Time Market (RTM)
```python
RTM_PARAMS = {
    "queryname": "PRC_RTPD_LMP",  # Real-time pre-dispatch LMP
    "market_run_id": "RTPD",
    "version": "12",
    "resultformat": "6",  # CSV format
    "node": "TH_SP15_GEN-APND",  # Southern California hub
}

# Alternative: 5-minute real-time
RTM_5MIN_PARAMS = {
    "queryname": "PRC_INTVL_LMP",
    "market_run_id": "RTM",
    "version": "12",
    "resultformat": "6"
}
```

#### Day-Ahead Market (DAM)
```python
DAM_PARAMS = {
    "queryname": "PRC_LMP",
    "market_run_id": "DAM",
    "version": "12",
    "resultformat": "6"
}
```

#### Fifteen Minute Market (FMM)
```python
FMM_PARAMS = {
    "queryname": "PRC_RTPD_LMP",
    "market_run_id": "FMM",
    "version": "12",
    "resultformat": "6"
}
```

### Node Selection Strategy

#### Primary California Nodes for V2G
```python
CALIFORNIA_NODES = {
    # Major hubs
    "TH_SP15_GEN-APND": "Southern California Hub",
    "TH_NP15_GEN-APND": "Northern California Hub",
    "TH_ZP26_GEN-APND": "Zone P26 Hub",
    
    # Load aggregation points
    "DLAP_SCE-APND": "Southern California Edison",
    "DLAP_PGAE-APND": "Pacific Gas & Electric",
    "DLAP_SDGE-APND": "San Diego Gas & Electric",
    
    # Specific locations (examples)
    "ALAMT_2_N001": "Alamitos",
    "BAYSHR_2_N001": "Bay Shore",
    "HUNTBCH_2_N022": "Huntington Beach"
}

def get_nearest_node(latitude: float, longitude: float) -> str:
    """Find nearest pricing node based on geographic location."""
    # Implementation would use node geographic mapping
    # For MVP, use default hub based on region
    if latitude < 36.0:  # Rough Southern California boundary
        return "TH_SP15_GEN-APND"
    else:
        return "TH_NP15_GEN-APND"
```

## Data Fetching Service

### Main Price Fetcher Class
```python
import asyncio
import aiohttp
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Optional
import zipfile
import io
from dataclasses import dataclass

@dataclass
class PriceData:
    timestamp: datetime
    node_id: str
    market_type: str
    lmp: float  # Locational Marginal Price
    energy_component: float
    congestion_component: float
    loss_component: float
    
class CAISOPriceFetcher:
    def __init__(self, redis_client, timescale_client):
        self.redis = redis_client
        self.timescale = timescale_client
        self.session: Optional[aiohttp.ClientSession] = None
        self.fetch_interval = 300  # 5 minutes
        
    async def start(self):
        """Start continuous price fetching."""
        self.session = aiohttp.ClientSession()
        
        # Schedule different market fetches
        asyncio.create_task(self.fetch_realtime_loop())
        asyncio.create_task(self.fetch_dayahead_loop())
        asyncio.create_task(self.fetch_fifteen_minute_loop())
        
    async def fetch_realtime_loop(self):
        """Fetch real-time prices every 5 minutes."""
        while True:
            try:
                await self.fetch_market_prices("RTPD")
                await asyncio.sleep(self.fetch_interval)
            except Exception as e:
                logger.error(f"RTM fetch failed: {e}")
                await asyncio.sleep(60)  # Retry after 1 minute
                
    async def fetch_market_prices(
        self,
        market_run_id: str,
        nodes: List[str] = None
    ) -> List[PriceData]:
        """Fetch prices for specified market and nodes."""
        
        if nodes is None:
            nodes = list(CALIFORNIA_NODES.keys())
        
        # Build query parameters
        params = {
            "queryname": self._get_query_name(market_run_id),
            "market_run_id": market_run_id,
            "version": "12",
            "resultformat": "6",
            "node": ",".join(nodes),
            "startdatetime": self._format_time(datetime.utcnow() - timedelta(hours=1)),
            "enddatetime": self._format_time(datetime.utcnow() + timedelta(hours=1))
        }
        
        # Fetch data
        async with self.session.get(
            CAISO_CONFIG["base_url"],
            params=params,
            timeout=aiohttp.ClientTimeout(total=CAISO_CONFIG["timeout"])
        ) as response:
            content = await response.read()
            
        # Parse ZIP response
        prices = await self._parse_oasis_response(content, market_run_id)
        
        # Store in Redis and TimescaleDB
        await self._store_prices(prices)
        
        return prices
    
    async def _parse_oasis_response(
        self,
        content: bytes,
        market_type: str
    ) -> List[PriceData]:
        """Parse OASIS ZIP response containing CSV data."""
        
        prices = []
        
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            for filename in zf.namelist():
                if filename.endswith('.csv'):
                    with zf.open(filename) as f:
                        df = pd.read_csv(f)
                        
                        # Parse CSV columns
                        for _, row in df.iterrows():
                            price = PriceData(
                                timestamp=pd.to_datetime(row['INTERVALSTARTTIME_GMT']),
                                node_id=row['NODE_ID_XML'],
                                market_type=market_type,
                                lmp=float(row['MW']),
                                energy_component=float(row.get('MEC', 0)),
                                congestion_component=float(row.get('MCC', 0)),
                                loss_component=float(row.get('MCL', 0))
                            )
                            prices.append(price)
        
        return prices
    
    def _format_time(self, dt: datetime) -> str:
        """Format datetime for OASIS API."""
        return dt.strftime("%Y%m%dT%H:%M-0000")
```

### Data Storage Pipeline

#### Redis Cache Management
```python
class PriceCacheManager:
    def __init__(self, redis_client):
        self.redis = redis_client
        self.ttl_current = 600  # 10 minutes for current prices
        self.ttl_forecast = 3600  # 1 hour for forecasts
        
    async def store_current_prices(self, prices: List[PriceData]):
        """Store current prices in Redis."""
        pipe = self.redis.pipeline()
        
        for price in prices:
            key = f"price:current:{price.node_id}"
            
            pipe.hset(key, mapping={
                "lmp": price.lmp,
                "energy": price.energy_component,
                "congestion": price.congestion_component,
                "loss": price.loss_component,
                "timestamp": price.timestamp.isoformat(),
                "market_type": price.market_type
            })
            pipe.expire(key, self.ttl_current)
        
        # Update aggregated hub prices
        hub_prices = self._calculate_hub_averages(prices)
        pipe.hset("prices:hubs:current", mapping=hub_prices)
        
        await pipe.execute()
    
    async def store_price_forecast(
        self,
        node_id: str,
        forecast: List[Tuple[datetime, float]]
    ):
        """Store price forecast as sorted set."""
        key = f"price:forecast:{node_id}"
        
        pipe = self.redis.pipeline()
        pipe.delete(key)
        
        for timestamp, price in forecast:
            score = timestamp.timestamp()
            value = json.dumps({
                "price": price,
                "timestamp": timestamp.isoformat()
            })
            pipe.zadd(key, {value: score})
        
        pipe.expire(key, self.ttl_forecast)
        await pipe.execute()
    
    def _calculate_hub_averages(self, prices: List[PriceData]) -> Dict:
        """Calculate weighted average prices for major hubs."""
        hub_groups = {
            "SP15": ["TH_SP15_GEN-APND", "DLAP_SCE-APND"],
            "NP15": ["TH_NP15_GEN-APND", "DLAP_PGAE-APND"],
            "ZP26": ["TH_ZP26_GEN-APND", "DLAP_SDGE-APND"]
        }
        
        hub_prices = {}
        for hub, nodes in hub_groups.items():
            hub_data = [p for p in prices if p.node_id in nodes]
            if hub_data:
                avg_price = sum(p.lmp for p in hub_data) / len(hub_data)
                hub_prices[hub] = avg_price
        
        return hub_prices
```

#### TimescaleDB Persistence
```python
class PriceTimescaleWriter:
    def __init__(self, connection_pool):
        self.pool = connection_pool
        
    async def bulk_insert_prices(self, prices: List[PriceData]):
        """Bulk insert prices into TimescaleDB."""
        
        async with self.pool.acquire() as conn:
            # Prepare data for COPY
            buffer = io.StringIO()
            
            for price in prices:
                buffer.write(f"{price.timestamp}\t")
                buffer.write(f"{price.node_id}\t")
                buffer.write(f"{price.market_type}\t")
                buffer.write(f"{price.lmp}\t")
                buffer.write(f"{price.energy_component}\t")
                buffer.write(f"{price.congestion_component}\t")
                buffer.write(f"{price.loss_component}\t")
                buffer.write(f"\\N\t")  # GHG adder (null for now)
                buffer.write(f"1.0\t")  # Price confidence
                buffer.write(f"0\n")    # Forecast horizon
            
            buffer.seek(0)
            
            # Use COPY for efficient insertion
            await conn.copy_to_table(
                'electricity_prices',
                source=buffer,
                columns=['time', 'node_id', 'market_type', 'lmp_price_mwh',
                        'energy_component_mwh', 'congestion_component_mwh',
                        'loss_component_mwh', 'ghg_adder_mwh',
                        'price_confidence', 'forecast_horizon_minutes']
            )
```

### Price Forecasting Service

#### Short-Term Price Prediction
```python
class PriceForecaster:
    def __init__(self, timescale_client):
        self.timescale = timescale_client
        self.models = {}  # Store trained models per node
        
    async def generate_forecast(
        self,
        node_id: str,
        horizon_hours: int = 24
    ) -> List[Tuple[datetime, float]]:
        """Generate price forecast using historical patterns."""
        
        # Fetch historical data
        historical = await self._fetch_historical_prices(node_id, days=30)
        
        # Extract features
        features = self._extract_features(historical)
        
        # Simple forecast using rolling average and seasonality
        forecast = []
        base_time = datetime.utcnow()
        
        for hour_offset in range(horizon_hours):
            forecast_time = base_time + timedelta(hours=hour_offset)
            
            # Get same hour from past days (seasonal pattern)
            same_hour_prices = [
                p.lmp for p in historical
                if p.timestamp.hour == forecast_time.hour
            ]
            
            if same_hour_prices:
                # Weighted average with recency bias
                weights = np.exp(-0.1 * np.arange(len(same_hour_prices)))
                weights = weights / weights.sum()
                predicted_price = np.average(same_hour_prices[::-1], weights=weights)
            else:
                predicted_price = np.mean([p.lmp for p in historical])
            
            # Add day-of-week adjustment
            dow_factor = self._get_dow_factor(forecast_time.weekday())
            predicted_price *= dow_factor
            
            forecast.append((forecast_time, predicted_price))
        
        return forecast
    
    def _get_dow_factor(self, day_of_week: int) -> float:
        """Get day-of-week price adjustment factor."""
        # Weekdays typically have higher prices
        factors = [1.1, 1.1, 1.1, 1.1, 1.0, 0.85, 0.85]  # Mon-Sun
        return factors[day_of_week]
```

### Event Publishing

#### Price Change Notifications
```python
class PriceEventPublisher:
    def __init__(self, redis_client, kafka_producer):
        self.redis = redis_client
        self.kafka = kafka_producer
        self.threshold_percent = 10  # Notify on 10% price changes
        
    async def check_and_notify_price_changes(
        self,
        new_prices: List[PriceData]
    ):
        """Check for significant price changes and notify."""
        
        for price in new_prices:
            # Get previous price
            prev_key = f"price:previous:{price.node_id}"
            prev_price = await self.redis.hget(prev_key, "lmp")
            
            if prev_price:
                prev_price = float(prev_price)
                change_percent = abs(price.lmp - prev_price) / prev_price * 100
                
                if change_percent > self.threshold_percent:
                    await self._publish_price_alert(price, prev_price, change_percent)
            
            # Update previous price
            await self.redis.hset(prev_key, mapping={
                "lmp": price.lmp,
                "timestamp": price.timestamp.isoformat()
            })
    
    async def _publish_price_alert(
        self,
        price: PriceData,
        previous: float,
        change_percent: float
    ):
        """Publish price change alert."""
        
        alert = {
            "event_type": "price_spike" if price.lmp > previous else "price_drop",
            "node_id": price.node_id,
            "current_price": price.lmp,
            "previous_price": previous,
            "change_percent": change_percent,
            "timestamp": price.timestamp.isoformat(),
            "market_type": price.market_type
        }
        
        # Publish to Redis
        await self.redis.publish(
            "notify:price:alert",
            json.dumps(alert)
        )
        
        # Publish to Kafka
        await self.kafka.send(
            "price.alerts",
            value=json.dumps(alert).encode(),
            key=price.node_id.encode()
        )
```

## Monitoring and Health Checks

### Service Health Monitoring
```python
class PriceFeederHealth:
    def __init__(self):
        self.last_fetch_time = {}
        self.fetch_failures = {}
        self.max_failures = 5
        
    async def health_check(self) -> Dict:
        """Comprehensive health check."""
        
        health = {
            "status": "healthy",
            "checks": {}
        }
        
        # Check CAISO API connectivity
        try:
            await self._test_caiso_api()
            health["checks"]["caiso_api"] = "ok"
        except Exception as e:
            health["checks"]["caiso_api"] = f"failed: {e}"
            health["status"] = "degraded"
        
        # Check data freshness
        for market in ["RTPD", "DAM", "FMM"]:
            if market in self.last_fetch_time:
                age_minutes = (datetime.utcnow() - self.last_fetch_time[market]).seconds / 60
                if age_minutes > 15:
                    health["checks"][f"{market}_freshness"] = f"stale: {age_minutes:.1f} minutes old"
                    health["status"] = "degraded"
                else:
                    health["checks"][f"{market}_freshness"] = "ok"
        
        # Check failure rates
        for market, failures in self.fetch_failures.items():
            if failures > self.max_failures:
                health["checks"][f"{market}_failures"] = f"excessive: {failures} failures"
                health["status"] = "unhealthy"
        
        return health
```

## Configuration and Deployment

### Environment Variables
```bash
# CAISO API Configuration
CAISO_DEFAULT_NODE=TH_SP15_GEN-APND
CAISO_FETCH_INTERVAL=300
CAISO_REQUEST_TIMEOUT=30
CAISO_MAX_RETRIES=3

# Price thresholds
PRICE_SPIKE_THRESHOLD=100  # $/MWh
PRICE_CHANGE_ALERT_PERCENT=10

# Service configuration
PRICE_FORECAST_HORIZON_HOURS=24
PRICE_CACHE_TTL=600
```

### Docker Deployment
```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY src/price_feeder/ ./src/price_feeder/

# Health check
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
  CMD curl -f http://localhost:8080/health || exit 1

# Run service
CMD ["python", "-m", "src.price_feeder.main"]
```

### Required Python Packages
```txt
aiohttp==3.9.1
aiokafka==0.10.0
asyncpg==0.29.0
pandas==2.1.4
numpy==1.26.2
redis==5.0.1
python-dateutil==2.8.2
pytz==2023.3
structlog==24.1.0
prometheus-client==0.19.0
```

## Integration Points

### Optimization Engine Interface
- Publish price updates to Redis channel `prices:update`
- Maintain 24-hour forecast in `price:forecast:{node_id}`
- Provide REST API endpoint for custom queries

### WebSocket Handler Integration
- Real-time price updates for dynamic charging decisions
- Price spike alerts trigger schedule recalculation

### Dashboard Requirements
- Expose Prometheus metrics at `/metrics`
- Provide WebSocket endpoint for real-time price stream
- Historical price API with aggregation options