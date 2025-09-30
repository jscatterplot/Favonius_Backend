#!/usr/bin/env python3
"""Database initialization script for Supabase integration."""

import asyncio
import os
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from websocket_handler.config import Config
from websocket_handler.database_schema import create_schema_from_config
from websocket_handler.monitoring import get_logger


async def main():
    """Initialize database schema."""
    logger = get_logger(__name__)
    
    try:
        # Load configuration
        config = Config.from_env()
        
        logger.info("Initializing database schema...")
        logger.info(f"Database host: {config.supabase.db_host}")
        logger.info(f"Database name: {config.supabase.db_name}")
        
        # Create schema
        await create_schema_from_config(config.supabase)
        
        logger.info("Database schema initialized successfully!")
        
    except Exception as e:
        logger.error(f"Failed to initialize database schema: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
