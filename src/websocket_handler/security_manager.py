"""OCPP Security Profile 3 implementation with mTLS, per-station auth tokens, and security event notification."""

import secrets
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes

from .monitoring import get_logger
from .timescale_client import TimescaleClient


class SecurityProfile(Enum):
    """OCPP Security Profiles."""

    PROFILE_1 = 1  # Basic HTTP authentication
    PROFILE_2 = 2  # TLS with server certificate
    PROFILE_3 = 3  # TLS with mutual authentication (mTLS)


class SecurityEventType(Enum):
    """Security event types."""

    FIRMWARE_UPDATED = "FirmwareUpdated"
    FAILED_TO_AUTHENTICATE_AT_CENTRAL_SYSTEM = "FailedToAuthenticateAtCentralSystem"
    CENTRAL_SYSTEM_FAILED_TO_AUTHENTICATE = "CentralSystemFailedToAuthenticate"
    SETTING_SYSTEM_TIME = "SettingSystemTime"
    STARTUP_OF_THE_DEVICE = "StartupOfTheDevice"
    RESET_OR_REBOOT = "ResetOrReboot"
    SECURITY_LOG_CLEARED = "SecurityLogCleared"
    RECONFIGURATION_SECURITY_PARAMETERS = "ReconfigurationSecurityParameters"
    MEMORY_EXHAUSTION = "MemoryExhaustion"
    INVALID_MESSAGES = "InvalidMessages"
    ATTEMPTED_REPLAY_ATTACKS = "AttemptedReplayAttacks"
    TAMPER_DETECTION_ACTIVATED = "TamperDetectionActivated"
    INVALID_FIRMWARE_SIGNATURE = "InvalidFirmwareSignature"
    INVALID_CERTIFICATE = "InvalidCertificate"
    CRITICAL_SECURITY_ERROR = "CriticalSecurityError"


class AuthenticationMethod(Enum):
    """Authentication methods."""

    BASIC_AUTH = "BasicAuth"
    BEARER_TOKEN = "BearerToken"
    CLIENT_CERTIFICATE = "ClientCertificate"
    API_KEY = "ApiKey"
    OAUTH2 = "OAuth2"


@dataclass
class SecurityEvent:
    """Security event data."""

    event_type: SecurityEventType
    timestamp: datetime
    tech_info: Optional[str] = None
    additional_info: Optional[Dict[str, Any]] = None


@dataclass
class StationAuthToken:
    """Station authentication token."""

    station_id: str
    token: str
    token_type: str
    expires_at: datetime
    created_at: datetime
    last_used: Optional[datetime] = None
    usage_count: int = 0


@dataclass
class SecurityConfig:
    """Security configuration."""

    security_profile: SecurityProfile = SecurityProfile.PROFILE_3
    require_mtls: bool = True
    require_station_auth: bool = True
    token_expiry_hours: int = 24
    max_failed_auth_attempts: int = 5
    lockout_duration_minutes: int = 30
    enable_audit_logging: bool = True
    require_secure_websocket: bool = True
    allowed_cipher_suites: List[str] = field(
        default_factory=lambda: [
            "TLS_AES_256_GCM_SHA384",
            "TLS_CHACHA20_POLY1305_SHA256",
            "TLS_AES_128_GCM_SHA256",
        ]
    )


class SecurityManager:
    """Manages OCPP Security Profile 3 features."""

    def __init__(self, timescale_client: TimescaleClient, config: SecurityConfig):
        self.timescale_client = timescale_client
        self.config = config
        self.logger = get_logger(__name__)

        # Token cache
        self.token_cache: Dict[str, StationAuthToken] = {}

        # Failed authentication tracking
        self.failed_auth_attempts: Dict[str, List[datetime]] = {}

        # Security event queue
        self.security_events: List[SecurityEvent] = []

        # JWT secret for token signing
        self.jwt_secret = secrets.token_urlsafe(32)

        # Certificate validation cache
        self.cert_validation_cache: Dict[str, Tuple[bool, datetime]] = {}

    async def authenticate_station(
        self,
        station_id: str,
        auth_data: Dict[str, Any],
        client_cert: Optional[x509.Certificate] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Authenticate station with multiple methods."""
        try:
            # Check if station is locked out
            if await self._is_station_locked_out(station_id):
                await self._log_security_event(
                    station_id,
                    SecurityEventType.FAILED_TO_AUTHENTICATE_AT_CENTRAL_SYSTEM,
                    f"Station {station_id} is locked out due to failed authentication attempts",
                )
                return False, "Station is temporarily locked out"

            basic_auth_required = await self._station_requires_basic_auth(station_id)
            if basic_auth_required:
                username = auth_data.get("username")
                password = auth_data.get("password")
                if not (username and password):
                    await self._record_failed_attempt(station_id)
                    await self._log_security_event(
                        station_id,
                        SecurityEventType.FAILED_TO_AUTHENTICATE_AT_CENTRAL_SYSTEM,
                        f"Basic Auth credentials are required for station {station_id}",
                        {"reason": "missing_basic_auth"},
                    )
                    return False, "Basic Auth credentials required"
                if username != station_id:
                    await self._record_failed_attempt(station_id)
                    await self._log_security_event(
                        station_id,
                        SecurityEventType.FAILED_TO_AUTHENTICATE_AT_CENTRAL_SYSTEM,
                        f"Basic Auth username must match station id for station {station_id}",
                        {"reason": "basic_auth_username_mismatch"},
                    )
                    return False, "Basic Auth username must match station id"

            # Production chargers provisioned through onboarding must use Basic Auth.
            auth_methods = (
                [(AuthenticationMethod.BASIC_AUTH, self._authenticate_basic_auth)]
                if basic_auth_required
                else [
                    (AuthenticationMethod.CLIENT_CERTIFICATE, self._authenticate_client_certificate),
                    (AuthenticationMethod.BEARER_TOKEN, self._authenticate_bearer_token),
                    (AuthenticationMethod.API_KEY, self._authenticate_api_key),
                    (AuthenticationMethod.BASIC_AUTH, self._authenticate_basic_auth),
                ]
            )

            for method, auth_func in auth_methods:
                if await auth_func(station_id, auth_data, client_cert):
                    # Successful authentication
                    await self._clear_failed_attempts(station_id)
                    await self._log_security_event(
                        station_id,
                        SecurityEventType.STARTUP_OF_THE_DEVICE,
                        f"Station {station_id} authenticated successfully using {method.value}",
                    )
                    return True, None

            # All authentication methods failed
            await self._record_failed_attempt(station_id)
            await self._log_security_event(
                station_id,
                SecurityEventType.FAILED_TO_AUTHENTICATE_AT_CENTRAL_SYSTEM,
                f"All authentication methods failed for station {station_id}",
                {"reason": "invalid_credentials"},
            )
            return False, "Authentication failed"

        except Exception as e:
            self.logger.error(f"Error authenticating station {station_id}: {e}")
            await self._log_security_event(
                station_id,
                SecurityEventType.CRITICAL_SECURITY_ERROR,
                f"Authentication error: {str(e)}",
            )
            return False, "Internal authentication error"

    async def generate_station_token(
        self, station_id: str, token_type: str = "Bearer"
    ) -> StationAuthToken:
        """Generate authentication token for station."""
        try:
            # Generate secure token
            token = jwt.encode(
                {
                    "station_id": station_id,
                    "token_type": token_type,
                    "iat": int(time.time()),
                    "exp": int(time.time()) + (self.config.token_expiry_hours * 3600),
                },
                self.jwt_secret,
                algorithm="HS256",
            )

            # Create token object
            auth_token = StationAuthToken(
                station_id=station_id,
                token=token,
                token_type=token_type,
                expires_at=datetime.now(timezone.utc)
                + timedelta(hours=self.config.token_expiry_hours),
                created_at=datetime.now(timezone.utc),
            )

            # Store in database
            await self._store_auth_token(auth_token)

            # Cache token
            self.token_cache[station_id] = auth_token

            self.logger.info(f"Generated {token_type} token for station {station_id}")
            return auth_token

        except Exception as e:
            self.logger.error(f"Error generating token for station {station_id}: {e}")
            raise

    async def validate_station_token(self, station_id: str, token: str) -> bool:
        """Validate station authentication token."""
        try:
            # Check cache first
            if station_id in self.token_cache:
                cached_token = self.token_cache[station_id]
                if cached_token.token == token and cached_token.expires_at > datetime.now(
                    timezone.utc
                ):
                    # Update usage
                    cached_token.last_used = datetime.now(timezone.utc)
                    cached_token.usage_count += 1
                    return True

            # Validate JWT token
            try:
                payload = jwt.decode(token, self.jwt_secret, algorithms=["HS256"])
                if payload.get("station_id") != station_id:
                    return False

                # Check expiration
                if payload.get("exp", 0) < time.time():
                    return False

                # Update token usage
                await self._update_token_usage(station_id, token)
                return True

            except jwt.InvalidTokenError:
                return False

        except Exception as e:
            self.logger.error(f"Error validating token for station {station_id}: {e}")
            return False

    async def revoke_station_token(self, station_id: str) -> bool:
        """Revoke station authentication token."""
        try:
            # Remove from cache
            self.token_cache.pop(station_id, None)

            # Mark as revoked in database
            await self._revoke_auth_token(station_id)

            await self._log_security_event(
                station_id,
                SecurityEventType.RECONFIGURATION_SECURITY_PARAMETERS,
                f"Authentication token revoked for station {station_id}",
            )

            self.logger.info(f"Revoked token for station {station_id}")
            return True

        except Exception as e:
            self.logger.error(f"Error revoking token for station {station_id}: {e}")
            return False

    async def handle_security_event_notification(
        self,
        station_id: str,
        event_type: str,
        timestamp: str,
        tech_info: Optional[str] = None,
        additional_info: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Handle SecurityEventNotification message."""
        try:
            # Parse event type
            try:
                security_event_type = SecurityEventType(event_type)
            except ValueError:
                return {
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": "PropertyConstraintViolation",
                        "additionalInfo": f"Unknown security event type: {event_type}",
                    },
                }

            # Create security event
            security_event = SecurityEvent(
                event_type=security_event_type,
                timestamp=datetime.fromisoformat(timestamp.replace("Z", "+00:00")),
                tech_info=tech_info,
                additional_info=additional_info,
            )

            # Store security event
            await self._store_security_event(station_id, security_event)

            # Handle critical events
            if security_event_type in [
                SecurityEventType.CRITICAL_SECURITY_ERROR,
                SecurityEventType.ATTEMPTED_REPLAY_ATTACKS,
                SecurityEventType.TAMPER_DETECTION_ACTIVATED,
            ]:
                await self._handle_critical_security_event(station_id, security_event)

            self.logger.info(f"Processed security event {event_type} from station {station_id}")

            return {"status": "Accepted"}

        except Exception as e:
            self.logger.error(f"Error handling security event: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {"reasonCode": "InternalError", "additionalInfo": str(e)},
            }

    async def get_security_events(
        self,
        station_id: Optional[str] = None,
        event_type: Optional[SecurityEventType] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> List[SecurityEvent]:
        """Get security events with filters."""
        try:
            events = await self.timescale_client.get_security_events(
                station_id, event_type.value if event_type else None, start_time, end_time
            )

            security_events = []
            for event_data in events:
                security_event = SecurityEvent(
                    event_type=SecurityEventType(event_data["event_type"]),
                    timestamp=event_data["timestamp"],
                    tech_info=event_data.get("tech_info"),
                    additional_info=event_data.get("additional_info"),
                )
                security_events.append(security_event)

            return security_events

        except Exception as e:
            self.logger.error(f"Error getting security events: {e}")
            return []

    async def create_ssl_context(
        self, cert_file: str, key_file: str, ca_file: Optional[str] = None
    ) -> ssl.SSLContext:
        """Create SSL context for mTLS."""
        try:
            # Create SSL context
            context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)

            # Load server certificate and key
            context.load_cert_chain(cert_file, key_file)

            # Load CA certificate for client verification
            if ca_file:
                context.load_verify_locations(ca_file)
                context.verify_mode = ssl.CERT_REQUIRED
                context.check_hostname = False  # We verify by certificate

            # Set cipher suites
            context.set_ciphers(":".join(self.config.allowed_cipher_suites))

            # Set minimum TLS version
            context.minimum_version = ssl.TLSVersion.TLSv1_2

            return context

        except Exception as e:
            self.logger.error(f"Error creating SSL context: {e}")
            raise

    async def validate_client_certificate(
        self, client_cert: x509.Certificate, station_id: str
    ) -> bool:
        """Validate client certificate for station."""
        try:
            # Check cache first
            cert_fingerprint = client_cert.fingerprint(hashes.SHA256()).hex()
            cache_key = f"{station_id}:{cert_fingerprint}"

            if cache_key in self.cert_validation_cache:
                is_valid, cached_time = self.cert_validation_cache[cache_key]
                if datetime.now(timezone.utc) - cached_time < timedelta(hours=1):
                    return is_valid

            # Validate certificate
            is_valid = await self._validate_certificate_chain(client_cert, station_id)

            # Cache result
            self.cert_validation_cache[cache_key] = (is_valid, datetime.now(timezone.utc))

            return is_valid

        except Exception as e:
            self.logger.error(f"Error validating client certificate: {e}")
            return False

    async def _authenticate_client_certificate(
        self, station_id: str, auth_data: Dict[str, Any], client_cert: Optional[x509.Certificate]
    ) -> bool:
        """Authenticate using client certificate."""
        if not client_cert or not self.config.require_mtls:
            return False

        return await self.validate_client_certificate(client_cert, station_id)

    async def _authenticate_bearer_token(
        self, station_id: str, auth_data: Dict[str, Any], client_cert: Optional[x509.Certificate]
    ) -> bool:
        """Authenticate using bearer token."""
        token = auth_data.get("bearer_token") or auth_data.get("authorization", "").replace(
            "Bearer ", ""
        )
        if not token:
            return False

        return await self.validate_station_token(station_id, token)

    async def _authenticate_api_key(
        self, station_id: str, auth_data: Dict[str, Any], client_cert: Optional[x509.Certificate]
    ) -> bool:
        """Authenticate using API key."""
        api_key = auth_data.get("api_key") or auth_data.get("x-api-key")
        if not api_key:
            return False

        # Validate API key against database
        return await self._validate_api_key(station_id, api_key)

    async def _authenticate_basic_auth(
        self, station_id: str, auth_data: Dict[str, Any], client_cert: Optional[x509.Certificate]
    ) -> bool:
        """Authenticate using basic authentication."""
        username = auth_data.get("username")
        password = auth_data.get("password")

        if not username or not password:
            return False

        # Validate credentials against database
        return await self._validate_basic_auth(station_id, username, password)

    async def _station_requires_basic_auth(self, station_id: str) -> bool:
        """Return True when a provisioned production charger requires Basic Auth."""
        checker = getattr(self.timescale_client, "station_requires_basic_auth", None)
        if checker is None:
            self.logger.warning(
                "Basic Auth requirement checker unavailable; allowing standard auth fallback for "
                "station %s",
                station_id,
            )
            return False
        try:
            return bool(await checker(station_id))
        except Exception as exc:
            self.logger.warning(
                "Could not check Basic Auth requirement for station %s: %s",
                station_id,
                exc,
            )
            return False

    async def _is_station_locked_out(self, station_id: str) -> bool:
        """Check if station is locked out due to failed attempts."""
        if station_id not in self.failed_auth_attempts:
            return False

        failed_attempts = self.failed_auth_attempts[station_id]
        recent_attempts = [
            attempt
            for attempt in failed_attempts
            if datetime.now(timezone.utc) - attempt
            < timedelta(minutes=self.config.lockout_duration_minutes)
        ]

        return len(recent_attempts) >= self.config.max_failed_auth_attempts

    async def _record_failed_attempt(self, station_id: str) -> None:
        """Record failed authentication attempt."""
        if station_id not in self.failed_auth_attempts:
            self.failed_auth_attempts[station_id] = []

        self.failed_auth_attempts[station_id].append(datetime.now(timezone.utc))

        # Clean old attempts
        cutoff_time = datetime.now(timezone.utc) - timedelta(
            minutes=self.config.lockout_duration_minutes
        )
        self.failed_auth_attempts[station_id] = [
            attempt for attempt in self.failed_auth_attempts[station_id] if attempt > cutoff_time
        ]

    async def _clear_failed_attempts(self, station_id: str) -> None:
        """Clear failed authentication attempts."""
        self.failed_auth_attempts.pop(station_id, None)

    async def _log_security_event(
        self,
        station_id: str,
        event_type: SecurityEventType,
        message: str,
        additional_info: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Log security event."""
        if not self.config.enable_audit_logging:
            return

        security_event = SecurityEvent(
            event_type=event_type,
            timestamp=datetime.now(timezone.utc),
            tech_info=message,
            additional_info=additional_info,
        )

        await self._store_security_event(station_id, security_event)

    async def _handle_critical_security_event(
        self, station_id: str, security_event: SecurityEvent
    ) -> None:
        """Handle critical security events."""
        # Revoke all tokens for the station
        await self.revoke_station_token(station_id)

        # Lock out the station temporarily
        self.failed_auth_attempts[station_id] = [
            datetime.now(timezone.utc) - timedelta(minutes=1)
        ] * self.config.max_failed_auth_attempts

        # Log the critical event
        self.logger.critical(
            f"Critical security event for station {station_id}: {security_event.event_type.value}"
        )

    async def _store_auth_token(self, auth_token: StationAuthToken) -> None:
        """Store authentication token in database."""
        await self.timescale_client.store_auth_token(
            {
                "station_id": auth_token.station_id,
                "token": auth_token.token,
                "token_type": auth_token.token_type,
                "expires_at": auth_token.expires_at,
                "created_at": auth_token.created_at,
            }
        )

    async def _update_token_usage(self, station_id: str, token: str) -> None:
        """Update token usage statistics."""
        await self.timescale_client.update_token_usage(
            station_id, token, datetime.now(timezone.utc)
        )

    async def _revoke_auth_token(self, station_id: str) -> None:
        """Revoke authentication token in database."""
        await self.timescale_client.revoke_auth_token(station_id, datetime.now(timezone.utc))

    async def _store_security_event(self, station_id: str, security_event: SecurityEvent) -> None:
        """Store security event in database."""
        await self.timescale_client.store_security_event(
            {
                "station_id": station_id,
                "event_type": security_event.event_type.value,
                "timestamp": security_event.timestamp,
                "tech_info": security_event.tech_info,
                "additional_info": security_event.additional_info,
            }
        )

    async def _validate_certificate_chain(
        self, client_cert: x509.Certificate, station_id: str
    ) -> bool:
        """Validate certificate chain."""
        try:
            # Check certificate validity period
            now = datetime.now(timezone.utc)
            if now < client_cert.not_valid_before or now > client_cert.not_valid_after:
                return False

            # Check if certificate is for the correct station
            subject_name = client_cert.subject.rfc4514_string()
            if station_id not in subject_name:
                return False

            # Additional validation can be added here (CRL, OCSP, etc.)
            return True

        except Exception as e:
            self.logger.error(f"Error validating certificate chain: {e}")
            return False

    async def _validate_api_key(self, station_id: str, api_key: str) -> bool:
        """Validate API key against database."""
        return await self.timescale_client.validate_api_key(station_id, api_key)

    async def _validate_basic_auth(self, station_id: str, username: str, password: str) -> bool:
        """Validate basic authentication credentials."""
        return await self.timescale_client.validate_basic_auth(station_id, username, password)
