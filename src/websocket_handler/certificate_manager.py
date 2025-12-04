"""ISO 15118 Certificate Management for V2G Plug & Charge."""

import asyncio
import base64
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Dict, List, Optional, Any, Set
import cryptography
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID

from .monitoring import get_logger
from .timescale_client import TimescaleClient
from .cache_manager import CacheManager


class CertificateType(Enum):
    """Certificate types for ISO 15118."""
    V2G_ROOT_CA = "V2GRootCertificate"
    MO_SUB_CA_1 = "MOSubCA1Certificate"
    MO_SUB_CA_2 = "MOSubCA2Certificate"
    OEM_SUB_CA_1 = "OEMSubCA1Certificate"
    OEM_SUB_CA_2 = "OEMSubCA2Certificate"
    CPO_SUB_CA_1 = "CPOSubCA1Certificate"
    CPO_SUB_CA_2 = "CPOSubCA2Certificate"
    V2G_CSMS_CERTIFICATE = "V2GCertificate"
    V2G_CERTIFICATE_CHAIN = "V2GCertificateChain"
    CONTRACT_CERTIFICATE = "ContractCertificate"
    CHARGING_STATION_CERTIFICATE = "ChargingStationCertificate"
    SECC_CERTIFICATE = "SECCCertificate"  # Supply Equipment Communication Controller
    V2G_ROOT_CERTIFICATE = "V2GRootCertificate"  # Alias for V2G_ROOT_CA
    MO_SUB_CERTIFICATE = "MOSubCertificate"  # Alias for MO_SUB_CA_1
    OEM_SUB_CERTIFICATE = "OEMSubCertificate"  # Alias for OEM_SUB_CA_1
    CPO_SUB_CERTIFICATE = "CPOSubCertificate"  # Alias for CPO_SUB_CA_1


class CertificateStatus(Enum):
    """Certificate status."""
    VALID = "Valid"
    EXPIRED = "Expired"
    REVOKED = "Revoked"
    PENDING = "Pending"
    INVALID = "Invalid"


@dataclass
class CertificateInfo:
    """Certificate information."""
    certificate_type: CertificateType
    certificate_data: str
    certificate_chain: Optional[List[str]] = None
    issuer_name: Optional[str] = None
    subject_name: Optional[str] = None
    serial_number: Optional[str] = None
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None
    status: CertificateStatus = CertificateStatus.VALID
    installation_date: Optional[datetime] = None


@dataclass
class CertificateInstallationResult:
    """Certificate installation result."""
    status: str
    status_info: Optional[Dict[str, Any]] = None


class CertificateManager:
    """Manages ISO 15118 certificates for V2G Plug & Charge."""
    
    def __init__(self, timescale_client: TimescaleClient, cache_manager: Optional[CacheManager] = None):
        self.timescale_client = timescale_client
        self.logger = get_logger(__name__)
        
        # Initialize cache manager
        self.cache_manager = cache_manager or CacheManager(max_size=500, default_ttl=timedelta(seconds=600))
        
        # Legacy certificate cache for backward compatibility
        self.certificate_cache: Dict[str, CertificateInfo] = {}
        
        # Certificate validation cache
        self.validation_cache: Dict[str, bool] = {}
        
        # Root CA certificates (loaded from configuration)
        self.root_cas: Dict[str, x509.Certificate] = {}
        
        # Certificate store limits
        self.certificate_limits = {
            CertificateType.V2G_ROOT_CA: 5,
            CertificateType.MO_SUB_CA_1: 10,
            CertificateType.MO_SUB_CA_2: 10,
            CertificateType.OEM_SUB_CA_1: 20,
            CertificateType.OEM_SUB_CA_2: 20,
            CertificateType.CPO_SUB_CA_1: 20,
            CertificateType.CPO_SUB_CA_2: 20,
            CertificateType.V2G_CSMS_CERTIFICATE: 10,
            CertificateType.V2G_CERTIFICATE_CHAIN: 50,
            CertificateType.CONTRACT_CERTIFICATE: 100,
            CertificateType.CHARGING_STATION_CERTIFICATE: 5
        }
    
    async def get_15118_ev_certificate(self, station_id: str, 
                                     certificate_type: CertificateType,
                                     exi_request: Optional[str] = None) -> Dict[str, Any]:
        """Get ISO 15118 EV certificate."""
        try:
            # Check if certificate exists
            certificate = await self._get_certificate(station_id, certificate_type)
            
            if certificate:
                return {
                    "status": "Accepted",
                    "exiResponse": certificate.certificate_data,
                    "statusInfo": {
                        "reasonCode": "NoError"
                    }
                }
            else:
                return {
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": "UnknownCertificate",
                        "additionalInfo": f"Certificate type {certificate_type.value} not found"
                    }
                }
                
        except Exception as e:
            self.logger.error(f"Error getting EV certificate: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {
                    "reasonCode": "InternalError",
                    "additionalInfo": str(e)
                }
            }
    
    async def certificate_signed(self, station_id: str, certificate_type: CertificateType,
                                certificate_chain: List[str], exi_response: str) -> Dict[str, Any]:
        """Handle CertificateSigned message."""
        try:
            # Validate certificate chain
            validation_result = await self._validate_certificate_chain(certificate_chain)
            
            if not validation_result["valid"]:
                return {
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": validation_result["reason_code"],
                        "additionalInfo": validation_result["message"]
                    }
                }
            
            # Parse certificate
            certificate_info = await self._parse_certificate(certificate_chain[0])
            
            # Check certificate store limits
            if not await self._check_certificate_limits(station_id, certificate_type):
                return {
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": "CertificateStoreMaxLengthExceeded",
                        "additionalInfo": f"Maximum {self.certificate_limits[certificate_type]} certificates allowed"
                    }
                }
            
            # Install certificate
            installation_result = await self._install_certificate(
                station_id, certificate_type, certificate_info, certificate_chain
            )
            
            if installation_result["status"] == "Accepted":
                self.logger.info(f"Installed {certificate_type.value} certificate for {station_id}")
            
            return installation_result
            
        except Exception as e:
            self.logger.error(f"Error handling certificate signed: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {
                    "reasonCode": "InternalError",
                    "additionalInfo": str(e)
                }
            }
    
    async def install_certificate(self, station_id: str, certificate_type: CertificateType,
                                certificate: str) -> Dict[str, Any]:
        """Install certificate."""
        try:
            # Convert string certificate type to enum if needed
            if isinstance(certificate_type, str):
                try:
                    certificate_type = CertificateType(certificate_type)
                except ValueError:
                    return {
                        "status": "Rejected",
                        "statusInfo": {
                            "reasonCode": "InvalidCertificateType",
                            "additionalInfo": f"Invalid certificate type: {certificate_type}"
                        }
                    }
            
            # Parse certificate
            certificate_info = await self._parse_certificate(certificate)
            
            # Validate certificate
            validation_result = await self._validate_certificate(certificate_info)
            
            if not validation_result["valid"]:
                return {
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": validation_result["reason_code"],
                        "additionalInfo": validation_result["message"]
                    }
                }
            
            # Check certificate store limits
            if not await self._check_certificate_limits(station_id, certificate_type):
                return {
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": "CertificateStoreMaxLengthExceeded",
                        "additionalInfo": f"Maximum {self.certificate_limits[certificate_type]} certificates allowed"
                    }
                }
            
            # Install certificate
            installation_result = await self._install_certificate(
                station_id, certificate_type, certificate_info, [certificate]
            )
            
            return installation_result
            
        except Exception as e:
            self.logger.error(f"Error installing certificate: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {
                    "reasonCode": "InternalError",
                    "additionalInfo": str(e)
                }
            }
    
    async def delete_certificate(self, station_id: str, certificate_hash_data: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Delete certificate(s)."""
        try:
            deleted_count = 0
            
            for hash_data in certificate_hash_data:
                certificate_type = CertificateType(hash_data["certificateType"])
                hash_algorithm = hash_data["hashAlgorithm"]
                issuer_name_hash = hash_data["issuerNameHash"]
                issuer_key_hash = hash_data["issuerKeyHash"]
                serial_number = hash_data["serialNumber"]
                
                # Find and delete certificate
                certificate_id = await self._find_certificate_by_hash(
                    station_id, certificate_type, hash_algorithm,
                    issuer_name_hash, issuer_key_hash, serial_number
                )
                
                if certificate_id:
                    await self._delete_certificate_by_id(station_id, certificate_id)
                    deleted_count += 1
            
            if deleted_count > 0:
                return {
                    "status": "Accepted",
                    "statusInfo": {
                        "reasonCode": "NoError"
                    }
                }
            else:
                return {
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": "UnknownCertificate",
                        "additionalInfo": "No matching certificates found"
                    }
                }
                
        except Exception as e:
            self.logger.error(f"Error deleting certificate: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {
                    "reasonCode": "InternalError",
                    "additionalInfo": str(e)
                }
            }
    
    async def get_installed_certificate_ids(self, station_id: str, 
                                          certificate_type: Optional[CertificateType] = None) -> Dict[str, Any]:
        """Get installed certificate IDs."""
        try:
            certificates = await self._get_installed_certificates(station_id, certificate_type)
            
            certificate_hash_data = []
            for cert in certificates:
                hash_data = await self._generate_certificate_hash(cert)
                certificate_hash_data.append(hash_data)
            
            return {
                "status": "Accepted",
                "certificateHashData": certificate_hash_data,
                "statusInfo": {
                    "reasonCode": "NoError"
                }
            }
            
        except Exception as e:
            self.logger.error(f"Error getting installed certificate IDs: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {
                    "reasonCode": "InternalError",
                    "additionalInfo": str(e)
                }
            }
    
    async def sign_certificate(self, station_id: str, certificate_type: CertificateType,
                             certificate_signing_request: str) -> Dict[str, Any]:
        """Sign certificate signing request."""
        try:
            # Parse CSR
            csr = x509.load_pem_x509_csr(certificate_signing_request.encode())
            
            # Validate CSR
            validation_result = await self._validate_csr(csr, certificate_type)
            
            if not validation_result["valid"]:
                return {
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": validation_result["reason_code"],
                        "additionalInfo": validation_result["message"]
                    }
                }
            
            # Sign certificate
            signed_certificate = await self._sign_certificate_request(csr, certificate_type)
            
            return {
                "status": "Accepted",
                "certificate": signed_certificate,
                "statusInfo": {
                    "reasonCode": "NoError"
                }
            }
            
        except Exception as e:
            self.logger.error(f"Error signing certificate: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {
                    "reasonCode": "InternalError",
                    "additionalInfo": str(e)
                }
            }
    
    async def _get_certificate(self, station_id: str, certificate_type: CertificateType) -> Optional[CertificateInfo]:
        """Get certificate from cache or database."""
        cache_key = f"{station_id}:{certificate_type.value}"
        
        # Check new cache manager first
        cached_cert = await self.cache_manager.get(cache_key)
        if cached_cert:
            self.logger.debug(f"Certificate {cache_key} found in cache")
            return cached_cert
        
        # Check legacy cache
        if cache_key in self.certificate_cache:
            return self.certificate_cache[cache_key]
        
        # Get from database
        certificate_data = await self.timescale_client.get_certificate(station_id, certificate_type.value)
        
        if certificate_data:
            certificate_info = CertificateInfo(
                certificate_type=certificate_type,
                certificate_data=certificate_data["certificate_data"],
                certificate_chain=json.loads(certificate_data.get("certificate_chain", "[]")),
                issuer_name=certificate_data.get("issuer_name"),
                subject_name=certificate_data.get("subject_name"),
                serial_number=certificate_data.get("serial_number"),
                valid_from=certificate_data.get("valid_from"),
                valid_to=certificate_data.get("valid_to"),
                status=CertificateStatus(certificate_data.get("status", "Valid")),
                installation_date=certificate_data.get("installation_date")
            )
            
            # Cache certificate in both caches
            await self.cache_manager.set(cache_key, certificate_info, ttl=timedelta(seconds=600))
            self.certificate_cache[cache_key] = certificate_info
            
            return certificate_info
        
        return None
    
    async def _parse_certificate(self, certificate_data: str) -> CertificateInfo:
        """Parse certificate data."""
        try:
            # Decode base64 certificate
            certificate_bytes = base64.b64decode(certificate_data)
            
            # Parse X.509 certificate
            certificate = x509.load_pem_x509_certificate(certificate_bytes)
            
            # Extract certificate information
            issuer_name = certificate.issuer.rfc4514_string()
            subject_name = certificate.subject.rfc4514_string()
            serial_number = str(certificate.serial_number)
            valid_from = certificate.not_valid_before_utc
            valid_to = certificate.not_valid_after_utc
            
            # Determine certificate type based on extensions
            certificate_type = await self._determine_certificate_type(certificate)
            
            return CertificateInfo(
                certificate_type=certificate_type,
                certificate_data=certificate_data,
                issuer_name=issuer_name,
                subject_name=subject_name,
                serial_number=serial_number,
                valid_from=valid_from,
                valid_to=valid_to,
                status=CertificateStatus.VALID,
                installation_date=datetime.now(timezone.utc)
            )
            
        except Exception as e:
            self.logger.error(f"Error parsing certificate: {e}")
            raise ValueError(f"Invalid certificate format: {e}")
    
    async def _validate_certificate_chain(self, certificate_chain: List[str]) -> Dict[str, Any]:
        """Validate certificate chain."""
        try:
            if not certificate_chain:
                return {
                    "valid": False,
                    "reason_code": "PropertyConstraintViolation",
                    "message": "Certificate chain cannot be empty"
                }
            
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
                    return {
                        "valid": False,
                        "reason_code": "CertificateChainError",
                        "message": f"Invalid signature in certificate chain at position {i}"
                    }
                
                # Check validity period
                now = datetime.now(timezone.utc)
                if now < issuer_cert.not_valid_before or now > issuer_cert.not_valid_after:
                    return {
                        "valid": False,
                        "reason_code": "CertificateExpired",
                        "message": f"Certificate at position {i + 1} is expired or not yet valid"
                    }
            
            return {"valid": True}
            
        except Exception as e:
            return {
                "valid": False,
                "reason_code": "CertificateChainError",
                "message": f"Certificate chain validation failed: {e}"
            }
    
    async def _validate_certificate(self, certificate_info: CertificateInfo) -> Dict[str, Any]:
        """Validate certificate."""
        try:
            # Check validity period
            now = datetime.now(timezone.utc)
            if certificate_info.valid_from and now < certificate_info.valid_from:
                return {
                    "valid": False,
                    "reason_code": "CertificateNotYetValid",
                    "message": "Certificate is not yet valid"
                }
            
            if certificate_info.valid_to and now > certificate_info.valid_to:
                return {
                    "valid": False,
                    "reason_code": "CertificateExpired",
                    "message": "Certificate has expired"
                }
            
            # Check certificate type specific validations
            if certificate_info.certificate_type == CertificateType.CONTRACT_CERTIFICATE:
                # Validate contract certificate specific requirements
                pass
            
            return {"valid": True}
            
        except Exception as e:
            return {
                "valid": False,
                "reason_code": "CertificateValidationError",
                "message": f"Certificate validation failed: {e}"
            }
    
    async def _check_certificate_limits(self, station_id: str, certificate_type: CertificateType) -> bool:
        """Check certificate store limits."""
        try:
            current_count = await self.timescale_client.count_certificates(station_id, certificate_type.value)
            max_count = self.certificate_limits[certificate_type]
            
            return current_count < max_count
            
        except Exception as e:
            self.logger.error(f"Error checking certificate limits: {e}")
            return False
    
    async def _install_certificate(self, station_id: str, certificate_type: CertificateType,
                                 certificate_info: CertificateInfo, certificate_chain: List[str]) -> Dict[str, Any]:
        """Install certificate."""
        try:
            # Store certificate in database
            await self.timescale_client.store_certificate({
                "station_id": station_id,
                "certificate_type": certificate_type.value,
                "certificate_data": certificate_info.certificate_data,
                "certificate_chain": json.dumps(certificate_chain),
                "issuer_name": certificate_info.issuer_name,
                "subject_name": certificate_info.subject_name,
                "serial_number": certificate_info.serial_number,
                "valid_from": certificate_info.valid_from,
                "valid_to": certificate_info.valid_to,
                "status": certificate_info.status.value,
                "installation_date": certificate_info.installation_date
            })
            
            # Update cache
            cache_key = f"{station_id}:{certificate_type.value}"
            self.certificate_cache[cache_key] = certificate_info
            
            return {
                "status": "Accepted",
                "statusInfo": {
                    "reasonCode": "NoError"
                }
            }
            
        except Exception as e:
            self.logger.error(f"Error installing certificate: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {
                    "reasonCode": "InternalError",
                    "additionalInfo": str(e)
                }
            }
    
    async def _determine_certificate_type(self, certificate: x509.Certificate) -> CertificateType:
        """Determine certificate type from X.509 certificate."""
        try:
            # Check extended key usage
            try:
                ext_key_usage = certificate.extensions.get_extension_for_oid(ExtendedKeyUsageOID.CLIENT_AUTH)
                if ext_key_usage:
                    return CertificateType.CONTRACT_CERTIFICATE
            except x509.ExtensionNotFound:
                pass
            
            # Check subject name patterns
            subject_name = certificate.subject.rfc4514_string()
            
            if "CN=V2G Root CA" in subject_name:
                return CertificateType.V2G_ROOT_CA
            elif "CN=MO Sub CA" in subject_name:
                return CertificateType.MO_SUB_CA_1
            elif "CN=OEM Sub CA" in subject_name:
                return CertificateType.OEM_SUB_CA_1
            elif "CN=CPO Sub CA" in subject_name:
                return CertificateType.CPO_SUB_CA_1
            elif "CN=V2G CSMS" in subject_name:
                return CertificateType.V2G_CSMS_CERTIFICATE
            elif "CN=Charging Station" in subject_name:
                return CertificateType.CHARGING_STATION_CERTIFICATE
            else:
                return CertificateType.V2G_CERTIFICATE_CHAIN
                
        except Exception:
            return CertificateType.V2G_CERTIFICATE_CHAIN
    
    async def _generate_certificate_hash(self, certificate_info: CertificateInfo) -> Dict[str, Any]:
        """Generate certificate hash data."""
        try:
            # Decode certificate
            cert_bytes = base64.b64decode(certificate_info.certificate_data)
            certificate = x509.load_pem_x509_certificate(cert_bytes)
            
            # Generate hashes
            issuer_name_hash = hashlib.sha256(certificate.issuer.public_bytes()).hexdigest()
            issuer_key_hash = hashlib.sha256(certificate.issuer.public_key().public_bytes(
                encoding=serialization.Encoding.DER,
                format=serialization.PublicFormat.SubjectPublicKeyInfo
            )).hexdigest()
            
            return {
                "certificateType": certificate_info.certificate_type.value,
                "hashAlgorithm": "SHA256",
                "issuerNameHash": issuer_name_hash,
                "issuerKeyHash": issuer_key_hash,
                "serialNumber": certificate_info.serial_number
            }
            
        except Exception as e:
            self.logger.error(f"Error generating certificate hash: {e}")
            raise
    
    async def _get_installed_certificates(self, station_id: str, 
                                        certificate_type: Optional[CertificateType]) -> List[CertificateInfo]:
        """Get installed certificates."""
        certificates_data = await self.timescale_client.get_installed_certificates(station_id, certificate_type.value if certificate_type else None)
        
        certificates = []
        for cert_data in certificates_data:
            certificate_info = CertificateInfo(
                certificate_type=CertificateType(cert_data["certificate_type"]),
                certificate_data=cert_data["certificate_data"],
                certificate_chain=json.loads(cert_data.get("certificate_chain", "[]")),
                issuer_name=cert_data.get("issuer_name"),
                subject_name=cert_data.get("subject_name"),
                serial_number=cert_data.get("serial_number"),
                valid_from=cert_data.get("valid_from"),
                valid_to=cert_data.get("valid_to"),
                status=CertificateStatus(cert_data.get("status", "Valid")),
                installation_date=cert_data.get("installation_date")
            )
            certificates.append(certificate_info)
        
        return certificates
    
    async def _find_certificate_by_hash(self, station_id: str, certificate_type: CertificateType,
                                      hash_algorithm: str, issuer_name_hash: str,
                                      issuer_key_hash: str, serial_number: str) -> Optional[str]:
        """Find certificate by hash data."""
        return await self.timescale_client.find_certificate_by_hash(
            station_id, certificate_type.value, hash_algorithm,
            issuer_name_hash, issuer_key_hash, serial_number
        )
    
    async def _delete_certificate_by_id(self, station_id: str, certificate_id: str) -> None:
        """Delete certificate by ID."""
        await self.timescale_client.delete_certificate(station_id, certificate_id)
    
    async def _validate_csr(self, csr: x509.CertificateSigningRequest, 
                           certificate_type: CertificateType) -> Dict[str, Any]:
        """Validate certificate signing request."""
        try:
            # Basic CSR validation
            if not csr.is_signature_valid:
                return {
                    "valid": False,
                    "reason_code": "InvalidCSR",
                    "message": "Invalid CSR signature"
                }
            
            # Check subject name
            subject_name = csr.subject.rfc4514_string()
            if not subject_name:
                return {
                    "valid": False,
                    "reason_code": "PropertyConstraintViolation",
                    "message": "CSR must have a subject name"
                }
            
            # Certificate type specific validation
            if certificate_type == CertificateType.CONTRACT_CERTIFICATE:
                # Validate contract certificate CSR
                pass
            
            return {"valid": True}
            
        except Exception as e:
            return {
                "valid": False,
                "reason_code": "InvalidCSR",
                "message": f"CSR validation failed: {e}"
            }
    
    async def _sign_certificate_request(self, csr: x509.CertificateSigningRequest,
                                      certificate_type: CertificateType) -> str:
        """Sign certificate request."""
        try:
            # This would integrate with your CA infrastructure
            # For now, return a placeholder
            return "PLACEHOLDER_SIGNED_CERTIFICATE"
            
        except Exception as e:
            self.logger.error(f"Error signing certificate request: {e}")
            raise
    
    # V2G-specific certificate validation methods
    async def validate_contract_certificate(self, station_id: str, contract_certificate: str, 
                                          emaid: Optional[str] = None) -> Dict[str, Any]:
        """Validate contract certificate for V2G operations."""
        try:
            self.logger.info(f"Validating contract certificate for station {station_id}")
            
            # Parse certificate
            certificate_info = await self._parse_certificate(contract_certificate)
            
            # Validate certificate chain
            if certificate_info.certificate_chain:
                chain_validation = await self._validate_certificate_chain(certificate_info.certificate_chain)
                if not chain_validation["valid"]:
                    return {
                        "valid": False,
                        "reason_code": chain_validation["reason_code"],
                        "message": chain_validation["message"]
                    }
            
            # Validate certificate
            cert_validation = await self._validate_certificate(certificate_info)
            if not cert_validation["valid"]:
                return cert_validation
            
            # Validate eMAID if provided
            if emaid:
                emaid_validation = await self._validate_emaid(certificate_info, emaid)
                if not emaid_validation["valid"]:
                    return emaid_validation
            
            # Check V2G-specific requirements
            v2g_validation = await self._validate_v2g_certificate_requirements(certificate_info)
            if not v2g_validation["valid"]:
                return v2g_validation
            
            return {
                "valid": True,
                "certificate_info": {
                    "subject_name": certificate_info.subject_name,
                    "issuer_name": certificate_info.issuer_name,
                    "serial_number": certificate_info.serial_number,
                    "valid_from": certificate_info.valid_from.isoformat() if certificate_info.valid_from else None,
                    "valid_to": certificate_info.valid_to.isoformat() if certificate_info.valid_to else None
                }
            }
            
        except Exception as e:
            self.logger.error(f"Error validating contract certificate: {e}")
            return {
                "valid": False,
                "reason_code": "InternalError",
                "message": str(e)
            }
    
    async def get_v2g_certificate_chain(self, station_id: str, certificate_type: CertificateType) -> Dict[str, Any]:
        """Get V2G certificate chain."""
        try:
            certificates = await self._get_installed_certificates(station_id, certificate_type)
            
            if not certificates:
                return {
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": "UnknownCertificate",
                        "additionalInfo": f"No {certificate_type.value} certificates found"
                    }
                }
            
            # Build certificate chain
            certificate_chain = []
            for cert in certificates:
                if cert.certificate_chain:
                    certificate_chain.extend(cert.certificate_chain)
                else:
                    certificate_chain.append(cert.certificate_data)
            
            return {
                "status": "Accepted",
                "certificateChain": certificate_chain,
                "statusInfo": {
                    "reasonCode": "NoError"
                }
            }
            
        except Exception as e:
            self.logger.error(f"Error getting V2G certificate chain: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {
                    "reasonCode": "InternalError",
                    "additionalInfo": str(e)
                }
            }
    
    async def validate_emaid(self, certificate_info: CertificateInfo, emaid: str) -> Dict[str, Any]:
        """Validate e-Mobility Account ID (eMAID) against certificate."""
        try:
            # Parse certificate to extract eMAID
            cert_bytes = base64.b64decode(certificate_info.certificate_data)
            certificate = x509.load_pem_x509_certificate(cert_bytes)
            
            # Extract eMAID from certificate extensions or subject
            cert_emaid = await self._extract_emaid_from_certificate(certificate)
            
            if not cert_emaid:
                return {
                    "valid": False,
                    "reason_code": "PropertyConstraintViolation",
                    "message": "eMAID not found in certificate"
                }
            
            if cert_emaid != emaid:
                return {
                    "valid": False,
                    "reason_code": "PropertyConstraintViolation",
                    "message": f"eMAID mismatch: expected {cert_emaid}, got {emaid}"
                }
            
            return {"valid": True}
            
        except Exception as e:
            self.logger.error(f"Error validating eMAID: {e}")
            return {
                "valid": False,
                "reason_code": "InternalError",
                "message": str(e)
            }
    
    async def handle_pnc_authorization(self, station_id: str, contract_certificate: str, 
                                    emaid: str, charging_needs: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Handle Plug & Charge authorization flow."""
        try:
            self.logger.info(f"Handling PnC authorization for station {station_id}, eMAID: {emaid}")
            
            # Validate contract certificate
            cert_validation = await self.validate_contract_certificate(station_id, contract_certificate, emaid)
            if not cert_validation["valid"]:
                return {
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": cert_validation["reason_code"],
                        "additionalInfo": cert_validation["message"]
                    }
                }
            
            # Check authorization status
            auth_status = await self._check_authorization_status(station_id, emaid)
            if not auth_status["authorized"]:
                return {
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": "NotAuthorized",
                        "additionalInfo": auth_status["reason"]
                    }
                }
            
            # Store authorization record
            await self._store_authorization_record(station_id, emaid, contract_certificate, charging_needs)
            
            return {
                "status": "Accepted",
                "statusInfo": {
                    "reasonCode": "NoError"
                },
                "authorization_data": {
                    "emaid": emaid,
                    "contract_id": cert_validation["certificate_info"]["serial_number"],
                    "valid_until": cert_validation["certificate_info"]["valid_to"]
                }
            }
            
        except Exception as e:
            self.logger.error(f"Error handling PnC authorization: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {
                    "reasonCode": "InternalError",
                    "additionalInfo": str(e)
                }
            }
    
    async def install_v2g_certificate(self, station_id: str, certificate_type: CertificateType,
                                    certificate: str, certificate_chain: Optional[List[str]] = None) -> Dict[str, Any]:
        """Install V2G-specific certificate."""
        try:
            # Parse certificate
            certificate_info = await self._parse_certificate(certificate)
            
            # Validate V2G-specific requirements
            v2g_validation = await self._validate_v2g_certificate_requirements(certificate_info)
            if not v2g_validation["valid"]:
                return {
                    "status": "Rejected",
                    "statusInfo": {
                        "reasonCode": v2g_validation["reason_code"],
                        "additionalInfo": v2g_validation["message"]
                    }
                }
            
            # Install certificate
            installation_result = await self._install_certificate(
                station_id, certificate_type, certificate_info, 
                certificate_chain or [certificate]
            )
            
            if installation_result["status"] == "Accepted":
                self.logger.info(f"Installed V2G {certificate_type.value} certificate for {station_id}")
            
            return installation_result
            
        except Exception as e:
            self.logger.error(f"Error installing V2G certificate: {e}")
            return {
                "status": "Rejected",
                "statusInfo": {
                    "reasonCode": "InternalError",
                    "additionalInfo": str(e)
                }
            }
    
    async def _validate_v2g_certificate_requirements(self, certificate_info: CertificateInfo) -> Dict[str, Any]:
        """Validate V2G-specific certificate requirements."""
        try:
            # Parse certificate
            cert_bytes = base64.b64decode(certificate_info.certificate_data)
            certificate = x509.load_pem_x509_certificate(cert_bytes)
            
            # Check extended key usage for V2G
            try:
                ext_key_usage = certificate.extensions.get_extension_for_oid(ExtendedKeyUsageOID.CLIENT_AUTH)
                if not ext_key_usage:
                    return {
                        "valid": False,
                        "reason_code": "PropertyConstraintViolation",
                        "message": "Certificate missing client authentication extended key usage"
                    }
            except x509.ExtensionNotFound:
                return {
                    "valid": False,
                    "reason_code": "PropertyConstraintViolation",
                    "message": "Certificate missing extended key usage extension"
                }
            
            # Check certificate type specific requirements
            if certificate_info.certificate_type == CertificateType.CONTRACT_CERTIFICATE:
                # Validate contract certificate specific requirements
                if not await self._validate_contract_certificate_requirements(certificate):
                    return {
                        "valid": False,
                        "reason_code": "PropertyConstraintViolation",
                        "message": "Contract certificate does not meet V2G requirements"
                    }
            
            return {"valid": True}
            
        except Exception as e:
            return {
                "valid": False,
                "reason_code": "CertificateValidationError",
                "message": f"V2G certificate validation failed: {e}"
            }
    
    async def _validate_contract_certificate_requirements(self, certificate: x509.Certificate) -> bool:
        """Validate contract certificate specific requirements."""
        try:
            # Check subject name format
            subject_name = certificate.subject.rfc4514_string()
            
            # Contract certificates should have specific subject name format
            # This is a simplified check - in practice, you'd have more specific requirements
            if not any(attr.oid == NameOID.COMMON_NAME for attr in certificate.subject):
                return False
            
            # Check validity period (contract certificates should be valid for reasonable period)
            validity_period = certificate.not_valid_after - certificate.not_valid_before
            if validity_period.days > 365 * 3:  # More than 3 years
                return False
            
            return True
            
        except Exception:
            return False
    
    async def _extract_emaid_from_certificate(self, certificate: x509.Certificate) -> Optional[str]:
        """Extract eMAID from certificate."""
        try:
            # Check subject alternative name extension
            try:
                san_ext = certificate.extensions.get_extension_for_oid(x509.oid.ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
                for name in san_ext.value:
                    if isinstance(name, x509.RFC822Name):
                        # eMAID might be in email format
                        if name.value.endswith("@emaid"):
                            return name.value
            except x509.ExtensionNotFound:
                pass
            
            # Check subject name
            for attr in certificate.subject:
                if attr.oid == NameOID.COMMON_NAME:
                    # eMAID might be in CN
                    if "@emaid" in attr.value:
                        return attr.value
            
            return None
            
        except Exception:
            return None
    
    async def _check_authorization_status(self, station_id: str, emaid: str) -> Dict[str, Any]:
        """Check authorization status for eMAID."""
        try:
            # This would integrate with your authorization system
            # For now, return authorized for all valid eMAIDs
            if emaid and "@emaid" in emaid:
                return {
                    "authorized": True,
                    "reason": "Valid eMAID"
                }
            else:
                return {
                    "authorized": False,
                    "reason": "Invalid eMAID format"
                }
                
        except Exception as e:
            self.logger.error(f"Error checking authorization status: {e}")
            return {
                "authorized": False,
                "reason": f"Authorization check failed: {e}"
            }
    
    async def _store_authorization_record(self, station_id: str, emaid: str, 
                                        contract_certificate: str, charging_needs: Optional[Dict[str, Any]]) -> None:
        """Store authorization record."""
        try:
            # Store authorization record in database
            auth_data = {
                "station_id": station_id,
                "emaid": emaid,
                "contract_certificate": contract_certificate,
                "charging_needs": json.dumps(charging_needs) if charging_needs else None,
                "authorization_time": datetime.now(timezone.utc),
                "status": "authorized"
            }
            
            await self.timescale_client.store_authorization_record(auth_data)
            
        except Exception as e:
            self.logger.error(f"Error storing authorization record: {e}")
