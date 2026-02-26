"""Secrets management for secure credential handling."""

import base64
import json
import os
from typing import Dict, Optional

import structlog
from cryptography.fernet import Fernet

from .config import BaseModel


class SecretsConfig(BaseModel):
    """Configuration for secrets management."""

    secrets_file: Optional[str] = None
    encryption_key: Optional[str] = None
    use_kubernetes_secrets: bool = True
    fallback_to_env: bool = True


class SecretsManager:
    """Manages application secrets securely."""

    def __init__(self, config: SecretsConfig):
        """Initialize secrets manager."""
        self.config = config
        self.logger = structlog.get_logger(__name__)
        self._secrets_cache: Dict[str, str] = {}
        self._fernet: Optional[Fernet] = None

        if config.encryption_key:
            self._setup_encryption()

    def _setup_encryption(self) -> None:
        """Setup encryption for local secrets file."""
        try:
            key = base64.urlsafe_b64decode(self.config.encryption_key.encode())
            self._fernet = Fernet(key)
        except Exception as e:
            self.logger.warning(f"Failed to setup encryption: {e}")
            self._fernet = None

    def get_secret(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get a secret value by key."""
        # Check cache first
        if key in self._secrets_cache:
            return self._secrets_cache[key]

        # Try Kubernetes secrets first
        if self.config.use_kubernetes_secrets:
            value = self._get_kubernetes_secret(key)
            if value:
                self._secrets_cache[key] = value
                return value

        # Try local secrets file
        if self.config.secrets_file:
            value = self._get_file_secret(key)
            if value:
                self._secrets_cache[key] = value
                return value

        # Fallback to environment variables
        if self.config.fallback_to_env:
            value = os.getenv(key)
            if value:
                self._secrets_cache[key] = value
                return value

        return default

    def _get_kubernetes_secret(self, key: str) -> Optional[str]:
        """Get secret from Kubernetes secret volume."""
        try:
            secret_path = f"/etc/secrets/{key}"
            if os.path.exists(secret_path):
                with open(secret_path, "r") as f:
                    return f.read().strip()
        except Exception as e:
            self.logger.debug(f"Failed to read Kubernetes secret {key}: {e}")
        return None

    def _get_file_secret(self, key: str) -> Optional[str]:
        """Get secret from encrypted local file."""
        try:
            if not os.path.exists(self.config.secrets_file):
                return None

            with open(self.config.secrets_file, "r") as f:
                data = json.load(f)

            encrypted_value = data.get(key)
            if not encrypted_value:
                return None

            # Decrypt if encryption is available
            if self._fernet:
                try:
                    return self._fernet.decrypt(encrypted_value.encode()).decode()
                except Exception:
                    # If decryption fails, assume it's plain text
                    return encrypted_value
            else:
                return encrypted_value

        except Exception as e:
            self.logger.debug(f"Failed to read file secret {key}: {e}")
        return None

    def set_secret(self, key: str, value: str, encrypt: bool = True) -> None:
        """Set a secret value (for local development)."""
        if not self.config.secrets_file:
            self.logger.warning("No secrets file configured, cannot set secret")
            return

        try:
            # Load existing secrets
            secrets = {}
            if os.path.exists(self.config.secrets_file):
                with open(self.config.secrets_file, "r") as f:
                    secrets = json.load(f)

            # Encrypt value if requested and encryption is available
            if encrypt and self._fernet:
                encrypted_value = self._fernet.encrypt(value.encode()).decode()
            else:
                encrypted_value = value

            secrets[key] = encrypted_value

            # Save secrets
            os.makedirs(os.path.dirname(self.config.secrets_file), exist_ok=True)
            with open(self.config.secrets_file, "w") as f:
                json.dump(secrets, f, indent=2)

            # Update cache
            self._secrets_cache[key] = value

        except Exception as e:
            self.logger.error(f"Failed to set secret {key}: {e}")
            raise

    def generate_encryption_key(self) -> str:
        """Generate a new encryption key for local secrets."""
        return Fernet.generate_key().decode()

    def create_kubernetes_secret_yaml(
        self, secrets: Dict[str, str], namespace: str = "ev-charging"
    ) -> str:
        """Generate Kubernetes secret YAML."""
        encoded_secrets = {}
        for key, value in secrets.items():
            encoded_secrets[key] = base64.b64encode(value.encode()).decode()

        yaml_content = f"""apiVersion: v1
kind: Secret
metadata:
  name: websocket-handler-secrets
  namespace: {namespace}
  labels:
    app.kubernetes.io/name: websocket-handler
    app.kubernetes.io/component: secrets
type: Opaque
data:
"""

        for key, value in encoded_secrets.items():
            yaml_content += f"  {key}: {value}\n"

        return yaml_content
