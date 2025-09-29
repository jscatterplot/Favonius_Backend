"""Main application entry point for OCPP WebSocket handler."""

import asyncio
import signal
import sys
import time
import uvloop
from typing import Optional

from .config import Config
from .server import OCPPWebSocketServer
from .health import HealthCheckServer
from .monitoring import setup_monitoring, setup_health_checks, get_logger


class Application:
    """Main application orchestrator."""
    
    def __init__(self, config: Config):
        """Initialize application."""
        self.config = config
        self.logger = get_logger(__name__)
        
        # Core components
        self.websocket_server: Optional[OCPPWebSocketServer] = None
        self.health_server: Optional[HealthCheckServer] = None
        
        # State
        self.running = False
        self.start_time = time.time()
        
    async def start(self) -> None:
        """Start all application components."""
        self.logger.info("Starting EV Charging WebSocket Handler...")
        
        try:
            # Setup monitoring first
            setup_monitoring(self.config.monitoring)
            
            # Create WebSocket server
            self.websocket_server = OCPPWebSocketServer(self.config)
            
            # Create health check server
            self.health_server = HealthCheckServer(self.config.monitoring.health_check_port)
            
            # Setup health checks after components are created
            if hasattr(self.websocket_server, 'redis_client') and hasattr(self.websocket_server, 'kafka_producer'):
                setup_health_checks(
                    self.websocket_server.redis_client,
                    self.websocket_server.kafka_producer,
                    self.websocket_server.connection_manager
                )
            
            # Start health server first
            await self.health_server.start()
            
            # Start WebSocket server
            self.running = True
            
            self.logger.info(
                f"Application started successfully in {time.time() - self.start_time:.2f}s"
            )
            
            # Start WebSocket server (this blocks until shutdown)
            await self.websocket_server.start()
            
        except Exception as e:
            self.logger.error(f"Failed to start application: {e}")
            raise
    
    async def stop(self) -> None:
        """Stop all application components gracefully."""
        self.logger.info("Shutting down application...")
        
        self.running = False
        
        # Stop components in reverse order
        stop_tasks = []
        
        if self.websocket_server:
            stop_tasks.append(self.websocket_server.stop())
        
        if self.health_server:
            stop_tasks.append(self.health_server.stop())
        
        # Wait for all components to stop
        if stop_tasks:
            await asyncio.gather(*stop_tasks, return_exceptions=True)
        
        self.logger.info("Application shutdown complete")
    
    def setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful shutdown."""
        def signal_handler(signum, frame):
            self.logger.info(f"Received signal {signum}, initiating shutdown...")
            
            # Create shutdown task
            loop = asyncio.get_event_loop()
            loop.create_task(self.stop())
        
        # Register signal handlers
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        # On Unix systems, also handle SIGHUP for graceful restart
        if hasattr(signal, 'SIGHUP'):
            signal.signal(signal.SIGHUP, signal_handler)


async def run_application(config: Config) -> None:
    """Run the application with proper setup."""
    # Use uvloop for better async performance on Unix systems
    if hasattr(uvloop, 'install'):
        uvloop.install()
    
    # Create application
    app = Application(config)
    
    # Setup signal handlers
    app.setup_signal_handlers()
    
    try:
        # Start application
        await app.start()
        
    except KeyboardInterrupt:
        app.logger.info("Received keyboard interrupt")
    except Exception as e:
        app.logger.error(f"Application error: {e}")
        raise
    finally:
        # Ensure cleanup happens
        await app.stop()


def main() -> None:
    """Main entry point."""
    # Load configuration from environment
    config = Config.from_env()
    
    # Setup basic logging for startup
    import logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    logger = logging.getLogger(__name__)
    
    try:
        # Validate configuration
        logger.info("Loading configuration...")
        logger.info(f"WebSocket server will bind to {config.websocket.host}:{config.websocket.port}")
        logger.info(f"Redis URL: {config.redis.url}")
        logger.info(f"Kafka brokers: {', '.join(config.kafka.brokers)}")
        logger.info(f"Environment: {config.environment}")
        
        if config.debug:
            logger.warning("Debug mode is enabled")
        
        # Run application
        logger.info("Starting application...")
        asyncio.run(run_application(config))
        
    except KeyboardInterrupt:
        logger.info("Application interrupted by user")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Application startup failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
