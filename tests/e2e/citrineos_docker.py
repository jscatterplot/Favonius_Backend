"""Docker setup for CitrineOS charging station simulation."""

import subprocess
import asyncio
import time
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class CitrineOSDockerSetup:
    """Setup CitrineOS using Docker."""

    def __init__(self, citrineos_path: str = "citrineos-core/Server/deploy.Dockerfile"):
        """Initialize Docker setup."""
        self.citrineos_path = Path(citrineos_path)
        self.container_name = "citrineos-server"
        self.image_name = "citrineos-server"
        self.port = 9000

    def build_image(self):
        """Build CitrineOS Docker image."""
        logger.info("Building CitrineOS Docker image...")
        
        try:
            # Build the Docker image
            cmd = [
                "docker", "build",
                "-f", str(self.citrineos_path),
                "-t", self.image_name,
                str(self.citrineos_path.parent)
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            logger.info("CitrineOS Docker image built successfully")
            return True
            
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to build Docker image: {e}")
            logger.error(f"Error output: {e.stderr}")
            return False

    def start_container(self):
        """Start CitrineOS container."""
        logger.info("Starting CitrineOS container...")
        
        try:
            # Stop existing container if running
            self.stop_container()
            
            # Start new container
            cmd = [
                "docker", "run",
                "-d",
                "--name", self.container_name,
                "-p", f"{self.port}:9000",
                "--network", "host",
                self.image_name
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            container_id = result.stdout.strip()
            logger.info(f"CitrineOS container started: {container_id}")
            return True
            
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to start container: {e}")
            logger.error(f"Error output: {e.stderr}")
            return False

    def stop_container(self):
        """Stop CitrineOS container."""
        logger.info("Stopping CitrineOS container...")
        
        try:
            # Stop container
            subprocess.run(["docker", "stop", self.container_name], 
                         capture_output=True, text=True)
            
            # Remove container
            subprocess.run(["docker", "rm", self.container_name], 
                         capture_output=True, text=True)
            
            logger.info("CitrineOS container stopped and removed")
            return True
            
        except subprocess.CalledProcessError as e:
            logger.warning(f"Failed to stop container: {e}")
            return False

    def check_container_status(self):
        """Check if container is running."""
        try:
            result = subprocess.run(
                ["docker", "ps", "--filter", f"name={self.container_name}", "--format", "{{.Status}}"],
                capture_output=True, text=True, check=True
            )
            
            status = result.stdout.strip()
            is_running = "Up" in status
            logger.info(f"Container status: {status}")
            return is_running
            
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to check container status: {e}")
            return False

    def get_container_logs(self):
        """Get container logs."""
        try:
            result = subprocess.run(
                ["docker", "logs", self.container_name],
                capture_output=True, text=True, check=True
            )
            
            logs = result.stdout
            logger.info(f"Container logs:\n{logs}")
            return logs
            
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to get container logs: {e}")
            return None

    def wait_for_service(self, timeout: int = 60):
        """Wait for service to be ready."""
        logger.info(f"Waiting for CitrineOS service to be ready (timeout: {timeout}s)...")
        
        start_time = time.time()
        while time.time() - start_time < timeout:
            if self.check_container_status():
                logger.info("CitrineOS service is ready")
                return True
            
            time.sleep(2)
        
        logger.error("CitrineOS service failed to start within timeout")
        return False

    def setup_citrineos(self):
        """Complete CitrineOS setup."""
        logger.info("Setting up CitrineOS...")
        
        # Build image
        if not self.build_image():
            return False
        
        # Start container
        if not self.start_container():
            return False
        
        # Wait for service
        if not self.wait_for_service():
            return False
        
        logger.info("CitrineOS setup completed successfully")
        return True

    def cleanup(self):
        """Cleanup CitrineOS setup."""
        logger.info("Cleaning up CitrineOS...")
        self.stop_container()
        logger.info("CitrineOS cleanup completed")


def setup_citrineos_docker():
    """Setup CitrineOS using Docker."""
    setup = CitrineOSDockerSetup()
    
    try:
        success = setup.setup_citrineos()
        if success:
            logger.info("CitrineOS Docker setup successful")
            return setup
        else:
            logger.error("CitrineOS Docker setup failed")
            return None
    except Exception as e:
        logger.error(f"CitrineOS Docker setup error: {e}")
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    setup_citrineos_docker()

