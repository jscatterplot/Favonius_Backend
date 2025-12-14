#!/bin/bash
# Favonius Energy - Deployment Verification Script
# Reference: PRD_v2.md Section 7.1, 10.1; Development Plan Phase 7

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
API_URL="${API_URL:-http://localhost:8000}"
OCPP_URL="${OCPP_URL:-ws://localhost:9000}"
TIMEOUT="${TIMEOUT:-10}"

echo "=========================================="
echo "Favonius Energy - Deployment Verification"
echo "=========================================="
echo ""

# Function to check HTTP endpoint
check_http() {
    local url=$1
    local description=$2
    local expected_status=${3:-200}
    
    echo -n "Checking $description... "
    
    if response=$(curl -s -w "\n%{http_code}" -m "$TIMEOUT" "$url" 2>/dev/null); then
        http_code=$(echo "$response" | tail -n1)
        body=$(echo "$response" | sed '$d')
        
        if [ "$http_code" -eq "$expected_status" ]; then
            echo -e "${GREEN}✓${NC} (HTTP $http_code)"
            return 0
        else
            echo -e "${RED}✗${NC} (HTTP $http_code, expected $expected_status)"
            return 1
        fi
    else
        echo -e "${RED}✗${NC} (Connection failed)"
        return 1
    fi
}

# Function to check health component
check_health_component() {
    local component=$1
    local health_response=$2
    
    if echo "$health_response" | grep -q "\"$component\":\"healthy\"" || \
       echo "$health_response" | grep -q "\"$component\":\"valid\""; then
        return 0
    else
        return 1
    fi
}

# Track failures
FAILURES=0

# 1. Health endpoint check
echo "1. Health Endpoint"
echo "   URL: $API_URL/health"
health_response=$(curl -s -m "$TIMEOUT" "$API_URL/health" 2>/dev/null || echo "")

if [ -z "$health_response" ]; then
    echo -e "   ${RED}✗ Health endpoint not reachable${NC}"
    FAILURES=$((FAILURES + 1))
else
    echo -e "   ${GREEN}✓ Health endpoint reachable${NC}"
    
    # Check individual components
    echo "   Checking components:"
    
    if check_health_component "database" "$health_response"; then
        echo -e "     ${GREEN}✓ Database: healthy${NC}"
    else
        echo -e "     ${RED}✗ Database: unavailable${NC}"
        FAILURES=$((FAILURES + 1))
    fi
    
    if check_health_component "ocpp_server" "$health_response"; then
        echo -e "     ${GREEN}✓ OCPP Server: healthy${NC}"
    else
        echo -e "     ${YELLOW}⚠ OCPP Server: unavailable (may be normal if no chargers connected)${NC}"
    fi
    
    if check_health_component "gurobi_license" "$health_response"; then
        echo -e "     ${GREEN}✓ Gurobi License: valid${NC}"
    else
        echo -e "     ${YELLOW}⚠ Gurobi License: invalid/unavailable (HiGHS fallback available)${NC}"
    fi
fi
echo ""

# 2. Metrics endpoint check
echo "2. Metrics Endpoint"
if check_http "$API_URL/metrics" "Metrics endpoint" 200; then
    echo "   ✓ Prometheus metrics available"
else
    echo -e "   ${YELLOW}⚠ Metrics endpoint not available${NC}"
fi
echo ""

# 3. API documentation check
echo "3. API Documentation"
if check_http "$API_URL/docs" "OpenAPI docs" 200; then
    echo "   ✓ API documentation available"
else
    echo -e "   ${YELLOW}⚠ API documentation not available${NC}"
fi
echo ""

# 4. Database connectivity (via health check)
echo "4. Database Connectivity"
if echo "$health_response" | grep -q "\"database\":\"healthy\""; then
    echo -e "   ${GREEN}✓ Database connection successful${NC}"
else
    echo -e "   ${RED}✗ Database connection failed${NC}"
    FAILURES=$((FAILURES + 1))
fi
echo ""

# 5. OCPP WebSocket (basic check)
echo "5. OCPP WebSocket Server"
echo "   URL: $OCPP_URL"
# Note: Full WebSocket check requires specialized tools
# This is a basic connectivity check
if timeout 2 bash -c "echo > /dev/tcp/$(echo $OCPP_URL | sed 's|ws://||' | cut -d: -f1)/$(echo $OCPP_URL | sed 's|ws://||' | cut -d: -f2 | cut -d/ -f1)" 2>/dev/null; then
    echo -e "   ${GREEN}✓ WebSocket port is open${NC}"
else
    echo -e "   ${YELLOW}⚠ WebSocket port check failed (may require WebSocket client)${NC}"
fi
echo ""

# Summary
echo "=========================================="
echo "Verification Summary"
echo "=========================================="

if [ $FAILURES -eq 0 ]; then
    echo -e "${GREEN}✓ All critical checks passed${NC}"
    echo ""
    echo "Deployment appears to be healthy."
    echo ""
    echo "Next steps:"
    echo "  - Verify OCPP charger connections"
    echo "  - Check optimization runs"
    echo "  - Monitor metrics in Prometheus/Grafana"
    exit 0
else
    echo -e "${RED}✗ $FAILURES critical check(s) failed${NC}"
    echo ""
    echo "Please review the errors above and fix deployment issues."
    exit 1
fi

