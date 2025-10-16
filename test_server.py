#!/usr/bin/env python3
"""Simple test server for load and e2e tests."""

import asyncio
import logging
import sys
import os

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from websocket_handler.config import Config, TimescaleConfig, SupabaseConfig
from websocket_handler.server import OCPPWebSocketServer
from websocket_handler.timescale_client import TimescaleClient

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def main():
    """Run a simple test server."""
    logger.info("Starting test server...")
    
    # Create minimal config
    config = Config(
        timescale=TimescaleConfig(
            service_url=os.getenv("TIMESCALE_SERVICE_URL", "postgres://test:test@localhost:5432/test"),
            host=os.getenv("PGHOST", "localhost"),
            port=int(os.getenv("PGPORT", "5432")),
            database=os.getenv("PGDATABASE", "test"),
            user=os.getenv("PGUSER", "test"),
            password=os.getenv("PGPASSWORD", "test")
        ),
        supabase=SupabaseConfig(
            url="https://test.supabase.co",
            anon_key="test_anon_key",
            service_key="test_service_key",
            db_host="test.db.host",
            db_user="test_user",
            db_password="test_password"
        )
    )
    
    # Create TimescaleDB client
    timescale_client = TimescaleClient(config.timescale)
    
    try:
        # Connect to database
        await timescale_client.connect()
        logger.info("Connected to TimescaleDB")
        
        # Create and start server
        server = OCPPWebSocketServer(config, timescale_client)
        await server.start()
        
        logger.info("Test server started on port 9000")
        
        # Keep running
        try:
            while True:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            
    except Exception as e:
        logger.error(f"Server error: {e}")
        return 1
    finally:
        await timescale_client.disconnect()
        
    return 0

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
