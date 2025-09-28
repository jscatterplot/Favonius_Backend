# Supabase Integration for V2G Platform

## Architecture Overview
Supabase provides authentication, real-time subscriptions, and PostgreSQL database for user-facing features, while TimescaleDB handles high-frequency telemetry data.

## Database Schema

### User Management Tables

#### organizations
```sql
CREATE TABLE organizations (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    type VARCHAR(50) CHECK (type IN ('fleet_operator', 'site_owner', 'utility')),
    billing_address JSONB,
    primary_contact JSONB,
    subscription_tier VARCHAR(50) DEFAULT 'starter',
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- RLS Policies
ALTER TABLE organizations ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view their organization"
    ON organizations FOR SELECT
    USING (id IN (
        SELECT organization_id FROM user_organizations 
        WHERE user_id = auth.uid()
    ));
```

#### user_organizations
```sql
CREATE TABLE user_organizations (
    user_id UUID REFERENCES auth.users(id) ON DELETE CASCADE,
    organization_id UUID REFERENCES organizations(id) ON DELETE CASCADE,
    role VARCHAR(50) CHECK (role IN ('owner', 'admin', 'operator', 'viewer')),
    joined_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (user_id, organization_id)
);

-- RLS Policies
ALTER TABLE user_organizations ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Users can view their memberships"
    ON user_organizations FOR SELECT
    USING (user_id = auth.uid());
```

#### sites
```sql
CREATE TABLE sites (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    organization_id UUID REFERENCES organizations(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    address JSONB NOT NULL,
    location GEOGRAPHY(POINT, 4326),
    utility_account_number VARCHAR(255),
    rate_schedule VARCHAR(100),
    max_power_kw DECIMAL(10,2),
    caiso_node_id VARCHAR(100),
    metadata JSONB,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Spatial index for location queries
CREATE INDEX idx_sites_location ON sites USING GIST(location);

-- RLS Policies
ALTER TABLE sites ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Organization members can view sites"
    ON sites FOR SELECT
    USING (organization_id IN (
        SELECT organization_id FROM user_organizations 
        WHERE user_id = auth.uid()
    ));
```

### Fleet Management Tables

#### vehicles
```sql
CREATE TABLE vehicles (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    organization_id UUID REFERENCES organizations(id) ON DELETE CASCADE,
    vin VARCHAR(17) UNIQUE NOT NULL,
    make VARCHAR(50),
    model VARCHAR(50),
    year INTEGER,
    battery_capacity_kwh DECIMAL(6,2),
    max_charge_rate_kw DECIMAL(6,2),
    max_discharge_rate_kw DECIMAL(6,2),
    v2g_capable BOOLEAN DEFAULT false,
    license_plate VARCHAR(20),
    driver_id UUID REFERENCES auth.users(id),
    status VARCHAR(50) DEFAULT 'active',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index for fleet queries
CREATE INDEX idx_vehicles_org ON vehicles(organization_id, status);

-- RLS Policies
ALTER TABLE vehicles ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Organization members can view vehicles"
    ON vehicles FOR SELECT
    USING (organization_id IN (
        SELECT organization_id FROM user_organizations 
        WHERE user_id = auth.uid()
    ));

CREATE POLICY "Admins can manage vehicles"
    ON vehicles FOR ALL
    USING (organization_id IN (
        SELECT organization_id FROM user_organizations 
        WHERE user_id = auth.uid() AND role IN ('owner', 'admin')
    ));
```

#### charging_stations
```sql
CREATE TABLE charging_stations (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    site_id UUID REFERENCES sites(id) ON DELETE CASCADE,
    station_id VARCHAR(255) UNIQUE NOT NULL,  -- OCPP identifier
    manufacturer VARCHAR(100),
    model VARCHAR(100),
    serial_number VARCHAR(255),
    firmware_version VARCHAR(50),
    ocpp_version VARCHAR(20),
    max_power_kw DECIMAL(8,2),
    connector_type VARCHAR(50),
    bidirectional BOOLEAN DEFAULT false,
    installation_date DATE,
    last_maintenance_date DATE,
    status VARCHAR(50) DEFAULT 'operational',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index for station lookups
CREATE INDEX idx_stations_site ON charging_stations(site_id, status);
CREATE UNIQUE INDEX idx_stations_ocpp_id ON charging_stations(station_id);
```

### Scheduling and Optimization Tables

#### charging_schedules_config
```sql
CREATE TABLE charging_schedules_config (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    organization_id UUID REFERENCES organizations(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    schedule_type VARCHAR(50) CHECK (schedule_type IN ('smart', 'time_of_use', 'solar_match', 'manual')),
    parameters JSONB NOT NULL,
    is_default BOOLEAN DEFAULT false,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Example parameters JSON:
-- {
--   "optimize_for": "cost",
--   "allow_v2g": true,
--   "min_departure_soc": 80,
--   "peak_avoidance": true,
--   "carbon_aware": false
-- }
```

#### vehicle_schedules
```sql
CREATE TABLE vehicle_schedules (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    vehicle_id UUID REFERENCES vehicles(id) ON DELETE CASCADE,
    schedule_config_id UUID REFERENCES charging_schedules_config(id),
    departure_time TIME,
    departure_soc_requirement DECIMAL(5,2),
    days_of_week INTEGER[], -- 0=Sunday, 6=Saturday
    priority INTEGER DEFAULT 5,
    override_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Index for schedule queries
CREATE INDEX idx_vehicle_schedules ON vehicle_schedules(vehicle_id, days_of_week);
```

### Analytics and Reporting Views

#### daily_energy_summary
```sql
CREATE OR REPLACE VIEW daily_energy_summary AS
SELECT 
    o.id as organization_id,
    o.name as organization_name,
    DATE(s.start_time) as date,
    COUNT(DISTINCT s.vehicle_id) as vehicles_charged,
    SUM(s.energy_delivered_kwh) as total_energy_charged,
    SUM(s.energy_received_kwh) as total_energy_discharged,
    AVG(s.session_duration_minutes) as avg_session_duration,
    SUM(s.cost_total) as total_cost,
    SUM(s.revenue_v2g) as total_v2g_revenue
FROM organizations o
LEFT JOIN charging_sessions_summary s ON o.id = s.organization_id
GROUP BY o.id, o.name, DATE(s.start_time);

-- Grant access
GRANT SELECT ON daily_energy_summary TO authenticated;
```

## Supabase Authentication Setup

### Auth Configuration
```javascript
// supabase/config.js
import { createClient } from '@supabase/supabase-js'

const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL
const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY

export const supabase = createClient(supabaseUrl, supabaseAnonKey, {
    auth: {
        autoRefreshToken: true,
        persistSession: true,
        detectSessionInUrl: true
    }
})
```

### Custom Auth Hooks
```javascript
// hooks/useAuth.js
import { useEffect, useState } from 'react'
import { supabase } from '../config/supabase'

export function useAuth() {
    const [user, setUser] = useState(null)
    const [organization, setOrganization] = useState(null)
    const [loading, setLoading] = useState(true)

    useEffect(() => {
        // Get initial session
        supabase.auth.getSession().then(({ data: { session } }) => {
            setUser(session?.user ?? null)
            if (session?.user) {
                fetchUserOrganization(session.user.id)
            }
            setLoading(false)
        })

        // Listen for auth changes
        const { data: { subscription } } = supabase.auth.onAuthStateChange(
            async (event, session) => {
                setUser(session?.user ?? null)
                if (session?.user) {
                    await fetchUserOrganization(session.user.id)
                } else {
                    setOrganization(null)
                }
            }
        )

        return () => subscription.unsubscribe()
    }, [])

    async function fetchUserOrganization(userId) {
        const { data, error } = await supabase
            .from('user_organizations')
            .select(`
                role,
                organizations (*)
            `)
            .eq('user_id', userId)
            .single()

        if (data) {
            setOrganization({
                ...data.organizations,
                userRole: data.role
            })
        }
    }

    return { user, organization, loading }
}
```

## Real-time Subscriptions

### Vehicle State Updates
```javascript
// Real-time vehicle tracking
function subscribeToVehicleUpdates(vehicleId) {
    return supabase
        .channel(`vehicle:${vehicleId}`)
        .on(
            'postgres_changes',
            {
                event: 'UPDATE',
                schema: 'public',
                table: 'vehicle_realtime_state',
                filter: `vehicle_id=eq.${vehicleId}`
            },
            (payload) => {
                updateVehicleState(payload.new)
            }
        )
        .subscribe()
}
```

### Fleet Dashboard Updates
```javascript
// Subscribe to fleet-wide updates
function subscribeToFleetUpdates(organizationId) {
    const channel = supabase.channel(`fleet:${organizationId}`)
    
    // Vehicle state changes
    channel.on(
        'postgres_changes',
        {
            event: '*',
            schema: 'public',
            table: 'vehicle_realtime_state',
            filter: `organization_id=eq.${organizationId}`
        },
        handleVehicleUpdate
    )
    
    // New charging sessions
    channel.on(
        'postgres_changes',
        {
            event: 'INSERT',
            schema: 'public',
            table: 'charging_sessions_active',
            filter: `organization_id=eq.${organizationId}`
        },
        handleNewSession
    )
    
    // Optimization decisions
    channel.on(
        'broadcast',
        { event: 'optimization_complete' },
        handleOptimizationUpdate
    )
    
    return channel.subscribe()
}
```

## API Functions

### Edge Functions
```typescript
// supabase/functions/start-charging/index.ts
import { serve } from 'https://deno.land/std@0.177.0/http/server.ts'
import { createClient } from '@supabase/supabase-js'

serve(async (req) => {
    try {
        const { vehicleId, chargingMode, targetSoc } = await req.json()
        
        // Verify user authorization
        const authHeader = req.headers.get('Authorization')
        const supabase = createClient(
            Deno.env.get('SUPABASE_URL'),
            Deno.env.get('SUPABASE_ANON_KEY'),
            { auth: { persistSession: false } }
        )
        
        const { data: { user }, error: authError } = await supabase.auth.getUser(
            authHeader?.replace('Bearer ', '')
        )
        
        if (authError) throw authError
        
        // Check vehicle ownership
        const { data: vehicle, error: vehicleError } = await supabase
            .from('vehicles')
            .select('*')
            .eq('id', vehicleId)
            .single()
        
        if (vehicleError) throw vehicleError
        
        // Send command to WebSocket service
        const response = await fetch(
            `${Deno.env.get('WEBSOCKET_SERVICE_URL')}/command`,
            {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-API-Key': Deno.env.get('INTERNAL_API_KEY')
                },
                body: JSON.stringify({
                    command: 'start_charging',
                    station_id: vehicle.current_station_id,
                    parameters: {
                        mode: chargingMode,
                        target_soc: targetSoc
                    }
                })
            }
        )
        
        if (!response.ok) throw new Error('Command failed')
        
        return new Response(
            JSON.stringify({ success: true }),
            { headers: { 'Content-Type': 'application/json' } }
        )
        
    } catch (error) {
        return new Response(
            JSON.stringify({ error: error.message }),
            { status: 400, headers: { 'Content-Type': 'application/json' } }
        )
    }
})
```

### Database Functions
```sql
-- Function to calculate cost savings
CREATE OR REPLACE FUNCTION calculate_savings(
    org_id UUID,
    start_date DATE,
    end_date DATE
) RETURNS TABLE (
    total_saved DECIMAL,
    v2g_revenue DECIMAL,
    demand_charge_reduction DECIMAL,
    tou_optimization_savings DECIMAL
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        SUM(baseline_cost - actual_cost) as total_saved,
        SUM(v2g_revenue) as v2g_revenue,
        SUM(demand_charge_baseline - demand_charge_actual) as demand_charge_reduction,
        SUM(tou_savings) as tou_optimization_savings
    FROM cost_analysis
    WHERE organization_id = org_id
        AND date BETWEEN start_date AND end_date;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;
```

## Storage Configuration

### Document Storage
```javascript
// Upload charging reports
async function uploadReport(file, organizationId) {
    const fileName = `reports/${organizationId}/${Date.now()}-${file.name}`
    
    const { data, error } = await supabase.storage
        .from('documents')
        .upload(fileName, file, {
            cacheControl: '3600',
            upsert: false
        })
    
    if (error) throw error
    
    // Save metadata
    const { error: dbError } = await supabase
        .from('reports')
        .insert({
            organization_id: organizationId,
            file_path: data.path,
            file_name: file.name,
            file_size: file.size,
            mime_type: file.type
        })
    
    return data.path
}
```

## Data Synchronization

### TimescaleDB to Supabase Sync
```python
# Python sync service
import asyncio
import asyncpg
from supabase import create_client

class DataSyncService:
    def __init__(self):
        self.timescale = None
        self.supabase = create_client(
            os.environ['SUPABASE_URL'],
            os.environ['SUPABASE_SERVICE_KEY']
        )
    
    async def sync_session_summaries(self):
        """Sync completed sessions from TimescaleDB to Supabase."""
        
        # Query completed sessions from TimescaleDB
        query = """
            SELECT 
                session_id,
                station_id,
                vehicle_id,
                organization_id,
                start_time,
                end_time,
                energy_delivered_kwh,
                energy_received_kwh,
                cost_total,
                revenue_v2g
            FROM charging_sessions
            WHERE end_time > NOW() - INTERVAL '1 hour'
                AND sync_status = 'pending'
        """
        
        sessions = await self.timescale.fetch(query)
        
        # Bulk insert to Supabase
        if sessions:
            session_data = [dict(s) for s in sessions]
            
            result = self.supabase.table('charging_sessions_summary') \
                .upsert(session_data) \
                .execute()
            
            # Mark as synced in TimescaleDB
            session_ids = [s['session_id'] for s in sessions]
            await self.timescale.execute(
                """
                UPDATE charging_sessions 
                SET sync_status = 'completed'
                WHERE session_id = ANY($1)
                """,
                session_ids
            )
```

## Security and Permissions

### Row Level Security Policies
```sql
-- Vehicles: Users can only see their organization's vehicles
CREATE POLICY "org_vehicles_select" ON vehicles
    FOR SELECT USING (
        organization_id IN (
            SELECT organization_id 
            FROM user_organizations 
            WHERE user_id = auth.uid()
        )
    );

-- Charging stations: Public read for available stations
CREATE POLICY "public_stations_view" ON charging_stations
    FOR SELECT USING (
        status = 'operational' 
        AND sites.is_public = true
    );

-- Private data: Strict organization boundaries
CREATE POLICY "private_data_access" ON charging_sessions_summary
    FOR ALL USING (
        organization_id IN (
            SELECT organization_id 
            FROM user_organizations 
            WHERE user_id = auth.uid()
                AND role IN ('owner', 'admin', 'operator')
        )
    );
```

### API Rate Limiting
```javascript
// Middleware for rate limiting
const rateLimit = {
    reads: {
        points: 1000,  // requests
        duration: 60,   // per minute
    },
    writes: {
        points: 100,
        duration: 60,
    }
}

// Applied via Supabase Edge Functions or API Gateway
```

## Monitoring and Analytics

### Usage Tracking
```sql
-- Track API usage per organization
CREATE TABLE api_usage (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    organization_id UUID REFERENCES organizations(id),
    endpoint VARCHAR(255),
    method VARCHAR(10),
    response_code INTEGER,
    response_time_ms INTEGER,
    timestamp TIMESTAMPTZ DEFAULT NOW()
);

-- Aggregate for billing
CREATE MATERIALIZED VIEW monthly_api_usage AS
SELECT 
    organization_id,
    DATE_TRUNC('month', timestamp) as month,
    COUNT(*) as total_requests,
    AVG(response_time_ms) as avg_response_time,
    COUNT(DISTINCT endpoint) as unique_endpoints
FROM api_usage
GROUP BY organization_id, DATE_TRUNC('month', timestamp);
```

## Environment Variables
```bash
# Supabase Configuration
NEXT_PUBLIC_SUPABASE_URL=https://yourproject.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=your-anon-key
SUPABASE_SERVICE_KEY=your-service-key

# Internal Services
WEBSOCKET_SERVICE_URL=wss://websocket.v2g.internal
OPTIMIZATION_SERVICE_URL=http://optimization.v2g.internal
TIMESCALE_SYNC_INTERVAL=300

# Feature Flags
ENABLE_V2G=true
ENABLE_REALTIME=true
ENABLE_ANALYTICS=true
```