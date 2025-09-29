#!/bin/sh

# Redis Cluster Node Entrypoint Script
# Configures and starts Redis cluster node with proper settings

set -e

echo "Starting Redis Cluster Node ${REDIS_NODE_ID}..."

# Set Redis configuration
CONFIG_FILE="/usr/local/etc/redis/redis.conf"
FINAL_CONFIG="/data/redis.conf"

# Copy base config
cp "$CONFIG_FILE" "$FINAL_CONFIG"

# Replace placeholders in config
if [ ! -z "$REDIS_ANNOUNCE_IP" ]; then
    sed -i "s/REDIS_ANNOUNCE_IP/$REDIS_ANNOUNCE_IP/g" "$FINAL_CONFIG"
fi

# Set password if provided
if [ ! -z "$REDIS_PASSWORD" ]; then
    echo "requirepass $REDIS_PASSWORD" >> "$FINAL_CONFIG"
    echo "masterauth $REDIS_PASSWORD" >> "$FINAL_CONFIG"
fi

# Ensure data directory permissions
chown -R redis:redis /data
chmod 755 /data

# Log configuration
echo "Redis configuration:"
echo "- Node ID: ${REDIS_NODE_ID}"
echo "- Announce IP: ${REDIS_ANNOUNCE_IP}"
echo "- Password: ${REDIS_PASSWORD:+****}"
echo "- Config file: $FINAL_CONFIG"

# Start Redis server
echo "Starting Redis server..."
exec redis-server "$FINAL_CONFIG"