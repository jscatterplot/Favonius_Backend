"""Security hardening and TLS implementation for V2G system."""

import os
import ssl
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from .config import TLSConfig
from .monitoring import get_logger


class CertificateManager:
    """Manages TLS certificates for V2G operations."""

    def __init__(self, tls_config: TLSConfig):
        """Initialize certificate manager."""
        self.tls_config = tls_config
        self.logger = get_logger(__name__)
        self.certificates: Dict[str, x509.Certificate] = {}
        self.private_keys: Dict[str, rsa.RSAPrivateKey] = {}

    def generate_self_signed_certificate(
        self,
        common_name: str,
        organization: str = "EV Charging V2G",
        country: str = "US",
        validity_days: int = 365,
    ) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
        """Generate a self-signed certificate for development/testing."""
        # Generate private key
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

        # Create certificate
        subject = issuer = x509.Name(
            [
                x509.NameAttribute(NameOID.COUNTRY_NAME, country),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, organization),
                x509.NameAttribute(NameOID.COMMON_NAME, common_name),
            ]
        )

        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(private_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.utcnow())
            .not_valid_after(datetime.utcnow() + timedelta(days=validity_days))
            .add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.DNSName("localhost"),
                        x509.IPAddress("127.0.0.1"),
                        x509.IPAddress("::1"),
                    ]
                ),
                critical=False,
            )
            .sign(private_key, hashes.SHA256())
        )

        return cert, private_key

    def save_certificate(
        self, cert: x509.Certificate, private_key: rsa.RSAPrivateKey, cert_path: str, key_path: str
    ) -> None:
        """Save certificate and private key to files."""
        # Save certificate
        with open(cert_path, "wb") as f:
            f.write(cert.public_bytes(serialization.Encoding.PEM))

        # Save private key
        with open(key_path, "wb") as f:
            f.write(
                private_key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption(),
                )
            )

        # Set secure file permissions
        os.chmod(cert_path, 0o644)
        os.chmod(key_path, 0o600)

        self.logger.info(f"Certificate saved to {cert_path}")
        self.logger.info(f"Private key saved to {key_path}")

    def load_certificate(self, cert_path: str) -> Optional[x509.Certificate]:
        """Load certificate from file."""
        try:
            with open(cert_path, "rb") as f:
                cert_data = f.read()
                cert = x509.load_pem_x509_certificate(cert_data)
                return cert
        except Exception as e:
            self.logger.error(f"Failed to load certificate from {cert_path}: {e}")
            return None

    def load_private_key(self, key_path: str) -> Optional[rsa.RSAPrivateKey]:
        """Load private key from file."""
        try:
            with open(key_path, "rb") as f:
                key_data = f.read()
                private_key = serialization.load_pem_private_key(key_data, password=None)
                return private_key
        except Exception as e:
            self.logger.error(f"Failed to load private key from {key_path}: {e}")
            return None

    def validate_certificate(self, cert: x509.Certificate) -> Dict[str, Any]:
        """Validate certificate and return validation results."""
        now = datetime.utcnow()

        validation_result = {"valid": True, "errors": [], "warnings": [], "info": {}}

        # Check validity period
        if now < cert.not_valid_before:
            validation_result["valid"] = False
            validation_result["errors"].append("Certificate not yet valid")
        elif now > cert.not_valid_after:
            validation_result["valid"] = False
            validation_result["errors"].append("Certificate expired")

        # Check expiration warning (30 days)
        if cert.not_valid_after - now < timedelta(days=30):
            validation_result["warnings"].append("Certificate expires within 30 days")

        # Check key size
        public_key = cert.public_key()
        if isinstance(public_key, rsa.RSAPublicKey):
            key_size = public_key.key_size
            if key_size < 2048:
                validation_result["warnings"].append(
                    f"RSA key size {key_size} is less than 2048 bits"
                )

        # Extract certificate info
        validation_result["info"] = {
            "subject": str(cert.subject),
            "issuer": str(cert.issuer),
            "serial_number": str(cert.serial_number),
            "not_valid_before": cert.not_valid_before.isoformat(),
            "not_valid_after": cert.not_valid_after.isoformat(),
            "key_size": key_size if isinstance(public_key, rsa.RSAPublicKey) else None,
        }

        return validation_result


class TLSServer:
    """TLS-enabled WebSocket server for V2G operations."""

    def __init__(self, tls_config: TLSConfig):
        """Initialize TLS server."""
        self.tls_config = tls_config
        self.logger = get_logger(__name__)
        self.cert_manager = CertificateManager(tls_config)
        self.ssl_context: Optional[ssl.SSLContext] = None

    def create_ssl_context(self) -> ssl.SSLContext:
        """Create SSL context for TLS connections."""
        # Create SSL context
        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)

        # Configure SSL context
        ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        ssl_context.maximum_version = ssl.TLSVersion.TLSv1_3

        # Set cipher suites (prioritize security)
        ssl_context.set_ciphers(
            "ECDHE+AESGCM:ECDHE+CHACHA20:DHE+AESGCM:DHE+CHACHA20:!aNULL:!MD5:!DSS"
        )

        # Load certificate and private key
        if self.tls_config.cert_path and self.tls_config.key_path:
            try:
                ssl_context.load_cert_chain(self.tls_config.cert_path, self.tls_config.key_path)
                self.logger.info("TLS certificate and key loaded successfully")
            except Exception as e:
                self.logger.error(f"Failed to load TLS certificate: {e}")
                raise
        else:
            # Generate self-signed certificate for development
            self.logger.warning("No TLS certificate provided, generating self-signed certificate")
            self._generate_development_certificate()
            ssl_context.load_cert_chain(self.tls_config.cert_path, self.tls_config.key_path)

        # Load CA certificate if provided
        if self.tls_config.ca_path:
            try:
                ssl_context.load_verify_locations(self.tls_config.ca_path)
                self.logger.info("CA certificate loaded successfully")
            except Exception as e:
                self.logger.error(f"Failed to load CA certificate: {e}")
                raise

        # Configure client certificate verification
        if self.tls_config.verify_client:
            ssl_context.verify_mode = ssl.CERT_REQUIRED
            ssl_context.check_hostname = True
        else:
            ssl_context.verify_mode = ssl.CERT_NONE
            ssl_context.check_hostname = False

        # Set additional security options
        ssl_context.options |= ssl.OP_NO_SSLv2
        ssl_context.options |= ssl.OP_NO_SSLv3
        ssl_context.options |= ssl.OP_NO_TLSv1
        ssl_context.options |= ssl.OP_NO_TLSv1_1
        ssl_context.options |= ssl.OP_SINGLE_DH_USE
        ssl_context.options |= ssl.OP_SINGLE_ECDH_USE

        self.ssl_context = ssl_context
        return ssl_context

    def _generate_development_certificate(self) -> None:
        """Generate development certificate and key."""
        cert_dir = Path("certs")
        cert_dir.mkdir(exist_ok=True)

        cert_path = cert_dir / "server.crt"
        key_path = cert_dir / "server.key"

        # Generate certificate
        cert, private_key = self.cert_manager.generate_self_signed_certificate(
            common_name="localhost", organization="EV Charging V2G Development", validity_days=365
        )

        # Save certificate and key
        self.cert_manager.save_certificate(cert, private_key, str(cert_path), str(key_path))

        # Update config
        self.tls_config.cert_path = str(cert_path)
        self.tls_config.key_path = str(key_path)

    def validate_tls_configuration(self) -> Dict[str, Any]:
        """Validate TLS configuration."""
        validation_result = {"valid": True, "errors": [], "warnings": [], "certificate_info": {}}

        # Check certificate file
        if self.tls_config.cert_path:
            if not os.path.exists(self.tls_config.cert_path):
                validation_result["valid"] = False
                validation_result["errors"].append(
                    f"Certificate file not found: {self.tls_config.cert_path}"
                )
            else:
                cert = self.cert_manager.load_certificate(self.tls_config.cert_path)
                if cert:
                    cert_validation = self.cert_manager.validate_certificate(cert)
                    validation_result["certificate_info"] = cert_validation["info"]
                    if not cert_validation["valid"]:
                        validation_result["valid"] = False
                        validation_result["errors"].extend(cert_validation["errors"])
                    validation_result["warnings"].extend(cert_validation["warnings"])

        # Check private key file
        if self.tls_config.key_path:
            if not os.path.exists(self.tls_config.key_path):
                validation_result["valid"] = False
                validation_result["errors"].append(
                    f"Private key file not found: {self.tls_config.key_path}"
                )
            else:
                # Check file permissions
                key_stat = os.stat(self.tls_config.key_path)
                if key_stat.st_mode & 0o077:  # Check if readable by others
                    validation_result["warnings"].append(
                        "Private key file has overly permissive permissions"
                    )

        # Check CA certificate file
        if self.tls_config.ca_path:
            if not os.path.exists(self.tls_config.ca_path):
                validation_result["valid"] = False
                validation_result["errors"].append(
                    f"CA certificate file not found: {self.tls_config.ca_path}"
                )

        return validation_result


class SecurityAuditor:
    """Security auditor for V2G system."""

    def __init__(self):
        """Initialize security auditor."""
        self.logger = get_logger(__name__)
        self.audit_results: Dict[str, Any] = {}

    def audit_system_security(self) -> Dict[str, Any]:
        """Perform comprehensive security audit."""
        self.logger.info("Starting security audit...")

        audit_results = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "overall_score": 0,
            "checks": {},
            "recommendations": [],
        }

        # Check 1: File permissions
        file_permissions_score = self._audit_file_permissions()
        audit_results["checks"]["file_permissions"] = file_permissions_score

        # Check 2: Environment variables
        env_security_score = self._audit_environment_security()
        audit_results["checks"]["environment_security"] = env_security_score

        # Check 3: Network security
        network_security_score = self._audit_network_security()
        audit_results["checks"]["network_security"] = network_security_score

        # Check 4: TLS configuration
        tls_security_score = self._audit_tls_security()
        audit_results["checks"]["tls_security"] = tls_security_score

        # Check 5: Database security
        db_security_score = self._audit_database_security()
        audit_results["checks"]["database_security"] = db_security_score

        # Calculate overall score
        scores = [score["score"] for score in audit_results["checks"].values()]
        audit_results["overall_score"] = sum(scores) / len(scores) if scores else 0

        # Generate recommendations
        audit_results["recommendations"] = self._generate_security_recommendations(audit_results)

        self.audit_results = audit_results
        return audit_results

    def _audit_file_permissions(self) -> Dict[str, Any]:
        """Audit file permissions."""
        score = 100
        issues = []

        # Check critical files
        critical_files = [
            "src/websocket_handler/config.py",
            "src/websocket_handler/secrets_manager.py",
            "requirements.txt",
        ]

        for file_path in critical_files:
            if os.path.exists(file_path):
                file_stat = os.stat(file_path)
                if file_stat.st_mode & 0o002:  # World writable
                    score -= 20
                    issues.append(f"File {file_path} is world writable")
                if file_stat.st_mode & 0o020:  # Group writable
                    score -= 10
                    issues.append(f"File {file_path} is group writable")

        return {
            "score": max(0, score),
            "issues": issues,
            "status": "pass" if score >= 80 else "fail",
        }

    def _audit_environment_security(self) -> Dict[str, Any]:
        """Audit environment security."""
        score = 100
        issues = []

        # Check for hardcoded secrets
        sensitive_patterns = ["password", "secret", "key", "token", "credential"]

        # Check environment variables
        for key, value in os.environ.items():
            if any(pattern in key.lower() for pattern in sensitive_patterns):
                if len(value) < 8:
                    score -= 15
                    issues.append(f"Environment variable {key} has weak value")
                if "password" in key.lower() and value == "password":
                    score -= 25
                    issues.append(f"Environment variable {key} has default value")

        return {
            "score": max(0, score),
            "issues": issues,
            "status": "pass" if score >= 80 else "fail",
        }

    def _audit_network_security(self) -> Dict[str, Any]:
        """Audit network security."""
        score = 100
        issues = []

        # Check for insecure protocols
        if "http://" in os.environ.get("SUPABASE_URL", ""):
            score -= 30
            issues.append("Supabase URL uses HTTP instead of HTTPS")

        if "sslmode=disable" in os.environ.get("TIMESCALE_SERVICE_URL", ""):
            score -= 25
            issues.append("Database connection uses SSL disabled")

        return {
            "score": max(0, score),
            "issues": issues,
            "status": "pass" if score >= 80 else "fail",
        }

    def _audit_tls_security(self) -> Dict[str, Any]:
        """Audit TLS security configuration."""
        score = 100
        issues = []

        # Check TLS configuration
        if not os.environ.get("TLS_CERT_PATH"):
            score -= 20
            issues.append("No TLS certificate configured")

        if not os.environ.get("TLS_KEY_PATH"):
            score -= 20
            issues.append("No TLS private key configured")

        if os.environ.get("TLS_VERIFY_CLIENT", "false").lower() != "true":
            score -= 15
            issues.append("Client certificate verification disabled")

        return {
            "score": max(0, score),
            "issues": issues,
            "status": "pass" if score >= 80 else "fail",
        }

    def _audit_database_security(self) -> Dict[str, Any]:
        """Audit database security."""
        score = 100
        issues = []

        # Check database connection security
        db_url = os.environ.get("TIMESCALE_SERVICE_URL", "")
        if "sslmode=require" not in db_url and "sslmode=prefer" not in db_url:
            score -= 25
            issues.append("Database connection not using SSL")

        # Check for weak passwords
        if "password" in db_url.lower():
            score -= 10
            issues.append("Password visible in connection string")

        return {
            "score": max(0, score),
            "issues": issues,
            "status": "pass" if score >= 80 else "fail",
        }

    def _generate_security_recommendations(self, audit_results: Dict[str, Any]) -> List[str]:
        """Generate security recommendations."""
        recommendations = []

        overall_score = audit_results["overall_score"]

        if overall_score < 80:
            recommendations.append(
                "Overall security score is below 80%. Address critical issues immediately."
            )

        for check_name, check_result in audit_results["checks"].items():
            if check_result["score"] < 80:
                recommendations.append(f"Improve {check_name}: {', '.join(check_result['issues'])}")

        # General recommendations
        recommendations.extend(
            [
                "Enable TLS for all communications",
                "Use strong, unique passwords for all accounts",
                "Regularly rotate certificates and keys",
                "Implement proper access controls",
                "Monitor security logs regularly",
                "Keep all dependencies updated",
                "Use secrets management for sensitive data",
            ]
        )

        return recommendations


class SecurityManager:
    """Centralized security management for V2G system."""

    def __init__(self, tls_config: TLSConfig):
        """Initialize security manager."""
        self.tls_config = tls_config
        self.logger = get_logger(__name__)
        self.tls_server = TLSServer(tls_config)
        self.security_auditor = SecurityAuditor()

    async def initialize_security(self) -> Dict[str, Any]:
        """Initialize security components."""
        self.logger.info("Initializing security components...")

        initialization_result = {
            "tls_initialized": False,
            "security_audit_passed": False,
            "errors": [],
        }

        try:
            # Initialize TLS
            self.tls_server.create_ssl_context()
            initialization_result["tls_initialized"] = True
            self.logger.info("TLS initialized successfully")

            # Run security audit
            audit_results = self.security_auditor.audit_system_security()
            initialization_result["security_audit_passed"] = audit_results["overall_score"] >= 80
            initialization_result["audit_results"] = audit_results

            if not initialization_result["security_audit_passed"]:
                initialization_result["errors"].append("Security audit failed")
                self.logger.warning("Security audit failed - review recommendations")

        except Exception as e:
            initialization_result["errors"].append(str(e))
            self.logger.error(f"Security initialization failed: {e}")

        return initialization_result

    def get_security_status(self) -> Dict[str, Any]:
        """Get current security status."""
        return {
            "tls_enabled": self.tls_config.cert_path is not None,
            "client_verification": self.tls_config.verify_client,
            "audit_results": self.security_auditor.audit_results,
            "recommendations": self.security_auditor.audit_results.get("recommendations", []),
        }
