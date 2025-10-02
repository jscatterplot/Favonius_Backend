#!/usr/bin/env python3
"""TimescaleDB initialization script for Tiger Cloud."""

import asyncio
import os
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from websocket_handler.config import Config
from websocket_handler.timescale_schema import create_timescale_schema_from_config
from websocket_handler.monitoring import get_logger


async def main():
    """Initialize TimescaleDB schema."""
    logger = get_logger(__name__)
    
    try:
        # Load configuration
        config = Config.from_env()
        
        logger.info("Initializing TimescaleDB schema...")
        logger.info(f"Database host: {config.timescale.host}")
        logger.info(f"Database name: {config.timescale.database}")
        logger.info(f"Service URL: {config.timescale.service_url}")
        
        # Create schema
        await create_timescale_schema_from_config(config.timescale)
        
        logger.info("TimescaleDB schema initialized successfully!")
        
    except Exception as e:
        logger.error(f"Failed to initialize TimescaleDB schema: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
