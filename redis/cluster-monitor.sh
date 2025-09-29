#!/bin/sh

# Redis Cluster Monitor Script
# Continuously monitors cluster health and provides status information

set -e

echo "Starting Redis Cluster Monitor..."

MONITOR_INTERVAL=30
CLUSTER_NODES="redis-cluster-1 redis-cluster-2 redis-cluster-3"

# Function to check node health
check_node_health() {
    local node=$1
    local port=${2:-6379}
    
    if redis-cli -h "$node" -p "$port" -a "$REDIS_PASSWORD" ping >/dev/null 2>&1; then
        return 0
    else
        return 1
    fi
}

# Function to get cluster info
get_cluster_info() {
    for node in $CLUSTER_NODES; do
        if check_node_health "$node"; then
            redis-cli -h "$node" -p 6379 -a "$REDIS_PASSWORD" cluster info
            return 0
        fi
    done
    echo "ERROR: No healthy cluster nodes found"
    return 1
}

# Function to get cluster nodes
get_cluster_nodes() {
    for node in $CLUSTER_NODES; do
        if check_node_health "$node"; then
            redis-cli -h "$node" -p 6379 -a "$REDIS_PASSWORD" cluster nodes
            return 0
        fi
    done
    echo "ERROR: No healthy cluster nodes found"
    return 1
}

# Function to check cluster health
check_cluster_health() {
    echo "=== Cluster Health Check - $(date) ==="
    
    # Check individual nodes
    echo "Node Health:"
    for i in 1 2 3 4 5 6; do
        node="redis-cluster-$i"
        if check_node_health "$node"; then
            # Get node role and memory info
            role=$(redis-cli -h "$node" -p 6379 -a "$REDIS_PASSWORD" info replication | grep "role:" | cut -d: -f2 | tr -d '\r')
            memory=$(redis-cli -h "$node" -p 6379 -a "$REDIS_PASSWORD" info memory | grep "used_memory_human:" | cut -d: -f2 | tr -d '\r')
            echo "  ✓ $node ($role) - Memory: $memory"
        else
            echo "  ✗ $node - UNREACHABLE"
        fi
    done
    
    # Get cluster state
    echo ""
    echo "Cluster State:"
    if cluster_info=$(get_cluster_info); then
        state=$(echo "$cluster_info" | grep "cluster_state:" | cut -d: -f2 | tr -d '\r')
        slots_assigned=$(echo "$cluster_info" | grep "cluster_slots_assigned:" | cut -d: -f2 | tr -d '\r')
        slots_ok=$(echo "$cluster_info" | grep "cluster_slots_ok:" | cut -d: -f2 | tr -d '\r')
        known_nodes=$(echo "$cluster_info" | grep "cluster_known_nodes:" | cut -d: -f2 | tr -d '\r')
        
        echo "  State: $state"
        echo "  Slots Assigned: $slots_assigned"
        echo "  Slots OK: $slots_ok" 
        echo "  Known Nodes: $known_nodes"
        
        if [ "$state" = "ok" ] && [ "$slots_assigned" = "16384" ] && [ "$slots_ok" = "16384" ]; then
            echo "  ✓ Cluster is healthy"
        else
            echo "  ⚠ Cluster has issues"
        fi
    else
        echo "  ✗ Unable to get cluster state"
    fi
    
    echo ""
    echo "=== End Health Check ==="
    echo ""
}

# Function to show cluster slots distribution
show_slots_distribution() {
    echo "=== Cluster Slots Distribution ==="
    if cluster_nodes=$(get_cluster_nodes); then
        echo "$cluster_nodes" | grep master | while read line; do
            node_id=$(echo "$line" | cut -d' ' -f1)
            node_address=$(echo "$line" | cut -d' ' -f2 | cut -d'@' -f1)
            slots=$(echo "$line" | cut -d' ' -f9-)
            echo "Node: $node_address ($node_id)"
            echo "Slots: $slots"
            echo ""
        done
    fi
    echo "=== End Slots Distribution ==="
    echo ""
}

# Function to monitor key distribution
monitor_key_distribution() {
    echo "=== Key Distribution Monitor ==="
    total_keys=0
    
    for i in 1 2 3 4 5 6; do
        node="redis-cluster-$i"
        if check_node_health "$node"; then
            keys=$(redis-cli -h "$node" -p 6379 -a "$REDIS_PASSWORD" info keyspace | grep "keys=" | wc -l)
            if [ "$keys" -gt 0 ]; then
                key_info=$(redis-cli -h "$node" -p 6379 -a "$REDIS_PASSWORD" info keyspace | grep "keys=")
                echo "  $node: $key_info"
                node_keys=$(echo "$key_info" | sed 's/.*keys=\([0-9]*\).*/\1/' | paste -sd+ | bc 2>/dev/null || echo 0)
                total_keys=$((total_keys + node_keys))
            else
                echo "  $node: no keys"
            fi
        fi
    done
    
    echo "  Total keys in cluster: $total_keys"
    echo "=== End Key Distribution ==="
    echo ""
}

# Main monitoring loop
echo "Starting continuous monitoring (interval: ${MONITOR_INTERVAL}s)"
echo "Press Ctrl+C to stop"
echo ""

# Initial detailed check
check_cluster_health
show_slots_distribution

# Continuous monitoring
while true; do
    sleep "$MONITOR_INTERVAL"
    
    # Basic health check
    healthy_nodes=0
    for i in 1 2 3 4 5 6; do
        if check_node_health "redis-cluster-$i"; then
            healthy_nodes=$((healthy_nodes + 1))
        fi
    done
    
    timestamp=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$timestamp] Healthy nodes: $healthy_nodes/6"
    
    # Detailed check every 5 minutes
    if [ $(($(date +%s) % 300)) -lt "$MONITOR_INTERVAL" ]; then
        check_cluster_health
        monitor_key_distribution
    fi
done