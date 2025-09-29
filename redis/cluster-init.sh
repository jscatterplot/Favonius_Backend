#!/bin/sh

# Redis Cluster Initialization Script
# Creates a Redis cluster with 3 masters and 3 replicas

set -e

echo "Initializing Redis Cluster..."

# Wait for all nodes to be ready
echo "Waiting for Redis nodes to be ready..."
for i in 1 2 3 4 5 6; do
    echo "Checking redis-cluster-$i..."
    while ! redis-cli -h redis-cluster-$i -p 6379 -a "$REDIS_PASSWORD" ping >/dev/null 2>&1; do
        echo "Waiting for redis-cluster-$i to be ready..."
        sleep 2
    done
    echo "redis-cluster-$i is ready"
done

echo "All nodes are ready. Creating cluster..."

# Create cluster with 3 masters and 3 replicas
redis-cli -a "$REDIS_PASSWORD" --cluster create \
    redis-cluster-1:6379 \
    redis-cluster-2:6379 \
    redis-cluster-3:6379 \
    redis-cluster-4:6379 \
    redis-cluster-5:6379 \
    redis-cluster-6:6379 \
    --cluster-replicas 1 \
    --cluster-yes

echo "Cluster creation completed successfully!"

# Verify cluster status
echo "Verifying cluster status..."
redis-cli -h redis-cluster-1 -p 6379 -a "$REDIS_PASSWORD" cluster info

echo "Cluster nodes:"
redis-cli -h redis-cluster-1 -p 6379 -a "$REDIS_PASSWORD" cluster nodes

# Test cluster functionality
echo "Testing cluster functionality..."
redis-cli -h redis-cluster-1 -p 6379 -a "$REDIS_PASSWORD" set test:cluster "Redis cluster is working!"
sleep 1
RESULT=$(redis-cli -h redis-cluster-2 -p 6379 -a "$REDIS_PASSWORD" get test:cluster)

if [ "$RESULT" = "Redis cluster is working!" ]; then
    echo "✓ Cluster test successful!"
    redis-cli -h redis-cluster-1 -p 6379 -a "$REDIS_PASSWORD" del test:cluster
else
    echo "✗ Cluster test failed!"
    exit 1
fi

echo "Redis cluster initialization completed successfully!"
echo ""
echo "Cluster endpoints:"
echo "- redis-cluster-1:6379"
echo "- redis-cluster-2:6379"  
echo "- redis-cluster-3:6379"
echo ""
echo "Use any of the master nodes as entry points."
echo "The cluster will automatically route commands to the correct node."