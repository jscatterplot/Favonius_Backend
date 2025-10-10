"""Plug & Charge (PnC) implementation with contract-based authorization and certificate chain validation."""

import asyncio
import base64
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Dict, List, Optional, Any, Set, Tuple
import cryptography
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID

from .monitoring import get_logger
from .timescale_client import TimescaleClient
from .certificate_manager import CertificateManager, CertificateType, CertificateInfo


class AuthorizationMethod(Enum):
    """Authorization methods for Plug & Charge."""
    EIM = "EIM"  # External Identification Means (RFID, app, etc.)
    PNC = "PnC"  # Plug & Charge (certificate-based)


class ContractStatus(Enum):
    """Contract status."""
    VALID = "Valid"
    EXPIRED = "Expired"
    REVOKED = "Revoked"
    SUSPENDED = "Suspended"
    PENDING = "Pending"


class PnCStatus(Enum):
    """Plug & Charge status."""
    ACCEPTED = "Accepted"
    REJECTED = "Rejected"
    BLOCKED = "Blocked"
    EXPIRED = "Expired"
    NO_CREDIT = "NoCredit"
    INVALID = "Invalid"


@dataclass
class ContractInfo:
    """Contract information for PnC."""
    contract_id: str
    ev_contract_id: str
    certificate_chain: List[str]
    contract_certificate: str
    valid_from: datetime
    valid_to: datetime
    status: ContractStatus
    energy_contract_id: Optional[str] = None
    tariff_id: Optional[str] = None
    max_power: Optional[float] = None
    max_energy: Optional[float] = None


@dataclass
class AuthorizationResult:
    """Authorization result for PnC."""
    status: PnCStatus
    contract_id: Optional[str] = None
    ev_contract_id: Optional[str] = None
    certificate_status: Optional[str] = None
    contract_status: Optional[str] = None
    energy_contract_id: Optional[str] = None
    tariff_id: Optional[str] = None
    max_power: Optional[float] = None
    max_energy: Optional[float] = None
    additional_info: Optional[Dict[str, Any]] = None


@dataclass
class PnCConfig:
    """Plug & Charge configuration."""
    enable_pnc: bool = True
    enable_eim: bool = True
    require_contract_validation: bool = True
    require_certificate_chain_validation: bool = True
    contract_cache_timeout_minutes: int = 60
    max_certificate_chain_length: int = 5
    enable_energy_contract_validation: bool = True
    enable_tariff_validation: bool = True


class PnCHandler:
    """Handles Plug & Charge authorization flow."""
    
    def __init__(self, timescale_client: TimescaleClient, certificate_manager: CertificateManager, config: PnCConfig):
        self.timescale_client = timescale_client
        self.certificate_manager = certificate_manager
        self.config = config
        self.logger = get_logger(__name__)
        
        # Contract cache
        self.contract_cache: Dict[str, ContractInfo] = {}
        
        # Certificate validation cache
        self.cert_validation_cache: Dict[str, Tuple[bool, datetime]] = {}
        
        # Authorization cache
        self.authorization_cache: Dict[str, Tuple[AuthorizationResult, datetime]] = {}
    
    async def authorize_plug_and_charge(self, station_id: str, evse_id: int,
                                      id_token: str, certificate_chain: Optional[List[str]] = None,
                                      contract_certificate: Optional[str] = None) -> AuthorizationResult:
        """Authorize Plug & Charge request."""
        try:
            # Determine authorization method
            auth_method = await self._determine_authorization_method(id_token, certificate_chain)
            
            if auth_method == AuthorizationMethod.PNC:
                return await self._authorize_pnc(station_id, evse_id, certificate_chain, contract_certificate)
            else:
                return await self._authorize_eim(station_id, evse_id, id_token)
                
        except Exception as e:
            self.logger.error(f"Error authorizing PnC for station {station_id}: {e}")
            return AuthorizationResult(
                status=PnCStatus.REJECTED,
                additional_info={"error": str(e)}
            )
    
    async def validate_contract_certificate(self, contract_certificate: str) -> Tuple[bool, Optional[ContractInfo]]:
        """Validate contract certificate and extract contract information."""
        try:
            # Parse certificate
            cert_bytes = base64.b64decode(contract_certificate)
            certificate = x509.load_pem_x509_certificate(cert_bytes)
            
            # Validate certificate
            if not await self._validate_certificate(certificate):
                return False, None
            
            # Extract contract information
            contract_info = await self._extract_contract_info(certificate)
            
            # Validate contract
            if not await self._validate_contract(contract_info):
                return False, None
            
            return True, contract_info
            
        except Exception as e:
            self.logger.error(f"Error validating contract certificate: {e}")
            return False, None
    
    async def validate_certificate_chain(self, certificate_chain: List[str]) -> bool:
        """Validate certificate chain for PnC."""
        try:
            if not certificate_chain:
                return False
            
            # Parse certificates
            certificates = []
            for cert_data in certificate_chain:
                cert_bytes = base64.b64decode(cert_data)
                cert = x509.load_pem_x509_certificate(cert_bytes)
                certificates.append(cert)
            
            # Validate chain
            for i in range(len(certificates) - 1):
                issuer_cert = certificates[i + 1]
                subject_cert = certificates[i]
                
                # Verify signature
                try:
                    issuer_cert.public_key().verify(
                        subject_cert.signature,
                        subject_cert.tbs_certificate_bytes,
                        padding.PKCS1v15(),
                        subject_cert.signature_hash_algorithm
                    )
                except Exception:
                    return False
                
                # Check validity period
                now = datetime.now(timezone.utc)
                if now < issuer_cert.not_valid_before or now > issuer_cert.not_valid_after:
                    return False
            
            # Check if root certificate is trusted
            root_cert = certificates[-1]
            return await self._is_trusted_root_certificate(root_cert)
            
        except Exception as e:
            self.logger.error(f"Error validating certificate chain: {e}")
            return False
    
    async def get_contract_info(self, contract_id: str) -> Optional[ContractInfo]:
        """Get contract information by ID."""
        try:
            # Check cache first
            if contract_id in self.contract_cache:
                cached_contract = self.contract_cache[contract_id]
                if cached_contract.valid_to > datetime.now(timezone.utc):
                    return cached_contract
            
            # Get from database
            contract_data = await self.timescale_client.get_contract_info(contract_id)
            if not contract_data:
                return None
            
            contract_info = ContractInfo(
                contract_id=contract_data["contract_id"],
                ev_contract_id=contract_data["ev_contract_id"],
                certificate_chain=json.loads(contract_data.get("certificate_chain", "[]")),
                contract_certificate=contract_data["contract_certificate"],
                valid_from=contract_data["valid_from"],
                valid_to=contract_data["valid_to"],
                status=ContractStatus(contract_data["status"]),
                energy_contract_id=contract_data.get("energy_contract_id"),
                tariff_id=contract_data.get("tariff_id"),
                max_power=contract_data.get("max_power"),
                max_energy=contract_data.get("max_energy")
            )
            
            # Cache contract
            self.contract_cache[contract_id] = contract_info
            
            return contract_info
            
        except Exception as e:
            self.logger.error(f"Error getting contract info: {e}")
            return None
    
    async def store_contract_info(self, contract_info: ContractInfo) -> None:
        """Store contract information."""
        try:
            # Store in database
            await self.timescale_client.store_contract_info({
                "contract_id": contract_info.contract_id,
                "ev_contract_id": contract_info.ev_contract_id,
                "certificate_chain": json.dumps(contract_info.certificate_chain),
                "contract_certificate": contract_info.contract_certificate,
                "valid_from": contract_info.valid_from,
                "valid_to": contract_info.valid_to,
                "status": contract_info.status.value,
                "energy_contract_id": contract_info.energy_contract_id,
                "tariff_id": contract_info.tariff_id,
                "max_power": contract_info.max_power,
                "max_energy": contract_info.max_energy
            })
            
            # Update cache
            self.contract_cache[contract_info.contract_id] = contract_info
            
            self.logger.info(f"Stored contract info for {contract_info.contract_id}")
            
        except Exception as e:
            self.logger.error(f"Error storing contract info: {e}")
    
    async def revoke_contract(self, contract_id: str, reason: str) -> bool:
        """Revoke contract."""
        try:
            # Update contract status
            await self.timescale_client.revoke_contract(contract_id, reason, datetime.now(timezone.utc))
            
            # Remove from cache
            self.contract_cache.pop(contract_id, None)
            
            self.logger.info(f"Revoked contract {contract_id}: {reason}")
            return True
            
        except Exception as e:
            self.logger.error(f"Error revoking contract: {e}")
            return False
    
    async def _determine_authorization_method(self, id_token: str, 
                                            certificate_chain: Optional[List[str]]) -> AuthorizationMethod:
        """Determine authorization method based on available data."""
        if certificate_chain and self.config.enable_pnc:
            return AuthorizationMethod.PNC
        elif id_token and self.config.enable_eim:
            return AuthorizationMethod.EIM
        else:
            # Default to EIM if both are available
            return AuthorizationMethod.EIM
    
    async def _authorize_pnc(self, station_id: str, evse_id: int,
                           certificate_chain: Optional[List[str]], 
                           contract_certificate: Optional[str]) -> AuthorizationResult:
        """Authorize using Plug & Charge."""
        try:
            # Validate certificate chain
            if certificate_chain and self.config.require_certificate_chain_validation:
                if not await self.validate_certificate_chain(certificate_chain):
                    return AuthorizationResult(
                        status=PnCStatus.REJECTED,
                        certificate_status="InvalidCertificateChain"
                    )
            
            # Validate contract certificate
            if contract_certificate:
                is_valid, contract_info = await self.validate_contract_certificate(contract_certificate)
                if not is_valid or not contract_info:
                    return AuthorizationResult(
                        status=PnCStatus.REJECTED,
                        certificate_status="InvalidContractCertificate"
                    )
                
                # Check contract status
                if contract_info.status != ContractStatus.VALID:
                    return AuthorizationResult(
                        status=PnCStatus.REJECTED,
                        contract_status=contract_info.status.value
                    )
                
                # Check contract validity period
                now = datetime.now(timezone.utc)
                if now < contract_info.valid_from or now > contract_info.valid_to:
                    return AuthorizationResult(
                        status=PnCStatus.EXPIRED,
                        contract_status="Expired"
                    )
                
                # Validate energy contract if enabled
                if self.config.enable_energy_contract_validation and contract_info.energy_contract_id:
                    if not await self._validate_energy_contract(contract_info.energy_contract_id):
                        return AuthorizationResult(
                            status=PnCStatus.REJECTED,
                            energy_contract_id=contract_info.energy_contract_id
                        )
                
                # Validate tariff if enabled
                if self.config.enable_tariff_validation and contract_info.tariff_id:
                    if not await self._validate_tariff(contract_info.tariff_id):
                        return AuthorizationResult(
                            status=PnCStatus.REJECTED,
                            tariff_id=contract_info.tariff_id
                        )
                
                # Check EVSE compatibility
                if not await self._check_evse_compatibility(station_id, evse_id, contract_info):
                    return AuthorizationResult(
                        status=PnCStatus.REJECTED,
                        additional_info={"reason": "EVSE not compatible with contract"}
                    )
                
                # Store contract info
                await self.store_contract_info(contract_info)
                
                return AuthorizationResult(
                    status=PnCStatus.ACCEPTED,
                    contract_id=contract_info.contract_id,
                    ev_contract_id=contract_info.ev_contract_id,
                    certificate_status="Valid",
                    contract_status="Valid",
                    energy_contract_id=contract_info.energy_contract_id,
                    tariff_id=contract_info.tariff_id,
                    max_power=contract_info.max_power,
                    max_energy=contract_info.max_energy
                )
            
            return AuthorizationResult(
                status=PnCStatus.REJECTED,
                certificate_status="NoContractCertificate"
            )
            
        except Exception as e:
            self.logger.error(f"Error in PnC authorization: {e}")
            return AuthorizationResult(
                status=PnCStatus.REJECTED,
                additional_info={"error": str(e)}
            )
    
    async def _authorize_eim(self, station_id: str, evse_id: int, id_token: str) -> AuthorizationResult:
        """Authorize using External Identification Means."""
        try:
            # Validate ID token
            token_info = await self.timescale_client.get_id_token_info(id_token, "ISO14443")
            
            if not token_info:
                return AuthorizationResult(
                    status=PnCStatus.REJECTED,
                    additional_info={"reason": "Unknown ID token"}
                )
            
            # Check token validity
            if token_info.get("expires_at") and token_info["expires_at"] < datetime.now(timezone.utc):
                return AuthorizationResult(
                    status=PnCStatus.EXPIRED,
                    additional_info={"reason": "ID token expired"}
                )
            
            # Check if token is blocked
            if token_info.get("blocked", False):
                return AuthorizationResult(
                    status=PnCStatus.BLOCKED,
                    additional_info={"reason": "ID token blocked"}
                )
            
            # Check credit/balance if applicable
            if token_info.get("require_credit", False):
                balance = await self._check_token_balance(id_token)
                if balance <= 0:
                    return AuthorizationResult(
                        status=PnCStatus.NO_CREDIT,
                        additional_info={"balance": balance}
                    )
            
            return AuthorizationResult(
                status=PnCStatus.ACCEPTED,
                additional_info={
                    "token_type": "EIM",
                    "charging_priority": token_info.get("charging_priority"),
                    "language": token_info.get("language1", "en")
                }
            )
            
        except Exception as e:
            self.logger.error(f"Error in EIM authorization: {e}")
            return AuthorizationResult(
                status=PnCStatus.REJECTED,
                additional_info={"error": str(e)}
            )
    
    async def _validate_certificate(self, certificate: x509.Certificate) -> bool:
        """Validate certificate."""
        try:
            # Check validity period
            now = datetime.now(timezone.utc)
            if now < certificate.not_valid_before or now > certificate.not_valid_after:
                return False
            
            # Check extended key usage
            try:
                ext_key_usage = certificate.extensions.get_extension_for_oid(ExtendedKeyUsageOID.CLIENT_AUTH)
                if not ext_key_usage:
                    return False
            except x509.ExtensionNotFound:
                return False
            
            return True
            
        except Exception as e:
            self.logger.error(f"Error validating certificate: {e}")
            return False
    
    async def _extract_contract_info(self, certificate: x509.Certificate) -> ContractInfo:
        """Extract contract information from certificate."""
        try:
            # Extract contract ID from subject
            subject_name = certificate.subject.rfc4514_string()
            
            # Parse contract ID from subject (format: CN=ContractID)
            contract_id = None
            for attribute in certificate.subject:
                if attribute.oid == NameOID.COMMON_NAME:
                    contract_id = attribute.value
                    break
            
            if not contract_id:
                raise ValueError("Contract ID not found in certificate subject")
            
            # Extract EV contract ID from subject alternative name
            ev_contract_id = contract_id  # Default to contract ID
            
            try:
                san_ext = certificate.extensions.get_extension_for_oid(x509.oid.ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
                for name in san_ext.value:
                    if isinstance(name, x509.RFC822Name):
                        ev_contract_id = name.value
                        break
            except x509.ExtensionNotFound:
                pass
            
            # Create contract info
            contract_info = ContractInfo(
                contract_id=contract_id,
                ev_contract_id=ev_contract_id,
                certificate_chain=[],
                contract_certificate=base64.b64encode(certificate.public_bytes(serialization.Encoding.PEM)).decode(),
                valid_from=certificate.not_valid_before,
                valid_to=certificate.not_valid_after,
                status=ContractStatus.VALID
            )
            
            return contract_info
            
        except Exception as e:
            self.logger.error(f"Error extracting contract info: {e}")
            raise
    
    async def _validate_contract(self, contract_info: ContractInfo) -> bool:
        """Validate contract."""
        try:
            # Check if contract exists in database
            existing_contract = await self.get_contract_info(contract_info.contract_id)
            
            if existing_contract:
                # Check if contract is revoked
                if existing_contract.status == ContractStatus.REVOKED:
                    return False
                
                # Update contract info with existing data
                contract_info.energy_contract_id = existing_contract.energy_contract_id
                contract_info.tariff_id = existing_contract.tariff_id
                contract_info.max_power = existing_contract.max_power
                contract_info.max_energy = existing_contract.max_energy
            
            return True
            
        except Exception as e:
            self.logger.error(f"Error validating contract: {e}")
            return False
    
    async def _is_trusted_root_certificate(self, root_cert: x509.Certificate) -> bool:
        """Check if root certificate is trusted."""
        try:
            # Get trusted root certificates from database
            trusted_roots = await self.timescale_client.get_trusted_root_certificates()
            
            for trusted_root in trusted_roots:
                if root_cert.fingerprint(hashes.SHA256()) == trusted_root.fingerprint(hashes.SHA256()):
                    return True
            
            return False
            
        except Exception as e:
            self.logger.error(f"Error checking trusted root certificate: {e}")
            return False
    
    async def _validate_energy_contract(self, energy_contract_id: str) -> bool:
        """Validate energy contract."""
        try:
            energy_contract = await self.timescale_client.get_energy_contract(energy_contract_id)
            if not energy_contract:
                return False
            
            # Check if energy contract is active
            if not energy_contract.get("active", False):
                return False
            
            # Check validity period
            now = datetime.now(timezone.utc)
            if energy_contract.get("valid_from") and now < energy_contract["valid_from"]:
                return False
            
            if energy_contract.get("valid_to") and now > energy_contract["valid_to"]:
                return False
            
            return True
            
        except Exception as e:
            self.logger.error(f"Error validating energy contract: {e}")
            return False
    
    async def _validate_tariff(self, tariff_id: str) -> bool:
        """Validate tariff."""
        try:
            tariff = await self.timescale_client.get_tariff_by_id(tariff_id)
            if not tariff:
                return False
            
            # Check if tariff is active
            if not tariff.get("active", False):
                return False
            
            # Check validity period
            now = datetime.now(timezone.utc)
            if tariff.get("valid_from") and now < tariff["valid_from"]:
                return False
            
            if tariff.get("valid_to") and now > tariff["valid_to"]:
                return False
            
            return True
            
        except Exception as e:
            self.logger.error(f"Error validating tariff: {e}")
            return False
    
    async def _check_evse_compatibility(self, station_id: str, evse_id: int, 
                                      contract_info: ContractInfo) -> bool:
        """Check if EVSE is compatible with contract."""
        try:
            # Get EVSE capabilities
            evse_capabilities = await self.timescale_client.get_evse_capabilities(station_id, evse_id)
            if not evse_capabilities:
                return False
            
            # Check power compatibility
            if contract_info.max_power:
                evse_max_power = evse_capabilities.get("max_power", 0)
                if contract_info.max_power > evse_max_power:
                    return False
            
            # Check connector type compatibility
            contract_connector_type = contract_info.additional_info.get("connector_type")
            if contract_connector_type:
                evse_connector_types = evse_capabilities.get("connector_types", [])
                if contract_connector_type not in evse_connector_types:
                    return False
            
            return True
            
        except Exception as e:
            self.logger.error(f"Error checking EVSE compatibility: {e}")
            return False
    
    async def _check_token_balance(self, id_token: str) -> float:
        """Check token balance."""
        try:
            balance_info = await self.timescale_client.get_token_balance(id_token)
            return balance_info.get("balance", 0.0) if balance_info else 0.0
            
        except Exception as e:
            self.logger.error(f"Error checking token balance: {e}")
            return 0.0
