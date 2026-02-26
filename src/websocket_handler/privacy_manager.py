"""GDPR Compliance and Privacy Management for OCPP 2.0.1."""

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from ocpp.v201.datatypes import IdTokenType, StatusInfoType
from ocpp.v201.enums import CustomerInformationStatusEnumType

from .monitoring import get_logger
from .timescale_client import TimescaleClient


class DataSubjectRight(Enum):
    """Data subject rights under GDPR."""

    ACCESS = "access"
    RECTIFICATION = "rectification"
    ERASURE = "erasure"
    PORTABILITY = "portability"
    RESTRICTION = "restriction"


class ConsentType(Enum):
    """Types of consent."""

    DATA_PROCESSING = "data_processing"
    MARKETING = "marketing"
    ANALYTICS = "analytics"
    THIRD_PARTY = "third_party"


class AnonymizationMethod(Enum):
    """PII anonymization methods."""

    HASH = "hash"
    MASK = "mask"
    DELETE = "delete"
    PSEUDONYMIZE = "pseudonymize"


class PrivacyManager:
    """Manages GDPR compliance and privacy features."""

    def __init__(self, timescale_client: TimescaleClient):
        """Initialize PrivacyManager."""
        self.timescale_client = timescale_client
        self.logger = get_logger(__name__)

    async def handle_customer_information_request(
        self,
        station_id: str,
        request_id: int,
        customer_certificate_id: Optional[str] = None,
        id_token: Optional[IdTokenType] = None,
        customer_identifier: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Handle CustomerInformation request."""
        self.logger.info(f"CustomerInformation request from {station_id}: {request_id}")

        try:
            # Store request
            request_data = {
                "request_id": str(request_id),
                "station_id": station_id,
                "customer_certificate_id": customer_certificate_id,
                "id_token": id_token.id_token if id_token else None,
                "customer_identifier": customer_identifier,
                "request_type": "CustomerInformation",
                "status": "processing",
                "requested_at": datetime.now(timezone.utc),
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }

            await self.timescale_client.store_customer_information_request(request_data)

            # Process request
            customer_info = await self._process_customer_information_request(
                station_id, customer_certificate_id, id_token, customer_identifier
            )

            # Update status
            await self.timescale_client.update_customer_information_status(
                str(request_id), "completed"
            )

            return {
                "status": CustomerInformationStatusEnumType.accepted,
                "customer_information": customer_info,
            }

        except Exception as e:
            self.logger.error(f"Error handling customer information request: {e}")
            await self.timescale_client.update_customer_information_status(
                str(request_id), "failed", str(e)
            )
            return {
                "status": CustomerInformationStatusEnumType.rejected,
                "statusInfo": StatusInfoType(reason_code="InternalError", additional_info=str(e)),
            }

    async def handle_delete_customer_information_request(
        self,
        station_id: str,
        request_id: int,
        customer_certificate_id: Optional[str] = None,
        id_token: Optional[IdTokenType] = None,
        customer_identifier: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Handle DeleteCustomerInformation request."""
        self.logger.info(f"DeleteCustomerInformation request from {station_id}: {request_id}")

        try:
            # Store request
            request_data = {
                "request_id": str(request_id),
                "station_id": station_id,
                "customer_certificate_id": customer_certificate_id,
                "id_token": id_token.id_token if id_token else None,
                "customer_identifier": customer_identifier,
                "request_type": "DeleteCustomerInformation",
                "status": "processing",
                "requested_at": datetime.now(timezone.utc),
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            }

            await self.timescale_client.store_customer_information_request(request_data)

            # Process deletion request
            await self._process_delete_customer_information_request(
                station_id, customer_certificate_id, id_token, customer_identifier
            )

            # Update status
            await self.timescale_client.update_customer_information_status(
                str(request_id), "completed"
            )

            return {"status": CustomerInformationStatusEnumType.accepted}

        except Exception as e:
            self.logger.error(f"Error handling delete customer information request: {e}")
            await self.timescale_client.update_customer_information_status(
                str(request_id), "failed", str(e)
            )
            return {
                "status": CustomerInformationStatusEnumType.rejected,
                "statusInfo": StatusInfoType(reason_code="InternalError", additional_info=str(e)),
            }

    async def create_data_retention_policy(
        self,
        data_type: str,
        retention_period_days: int,
        anonymization_required: bool = False,
        deletion_method: str = "soft",
        description: Optional[str] = None,
    ) -> str:
        """Create data retention policy."""
        policy_id = str(uuid.uuid4())

        policy_data = {
            "policy_id": policy_id,
            "data_type": data_type,
            "retention_period_days": retention_period_days,
            "anonymization_required": anonymization_required,
            "deletion_method": deletion_method,
            "policy_description": description,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }

        await self.timescale_client.store_data_retention_policy(policy_data)
        self.logger.info(f"Created data retention policy {policy_id} for {data_type}")

        return policy_id

    async def create_default_retention_policies(self) -> None:
        """Create default data retention policies."""
        policies = [
            {
                "data_type": "transaction_data",
                "retention_period_days": 2555,  # 7 years for financial records
                "anonymization_required": True,
                "deletion_method": "anonymize",
                "description": "Transaction data retention for financial compliance",
            },
            {
                "data_type": "meter_values",
                "retention_period_days": 365,  # 1 year for meter readings
                "anonymization_required": False,
                "deletion_method": "soft",
                "description": "Meter values retention for billing and analytics",
            },
            {
                "data_type": "logs",
                "retention_period_days": 90,  # 3 months for system logs
                "anonymization_required": True,
                "deletion_method": "hard",
                "description": "System logs retention for debugging and compliance",
            },
            {
                "data_type": "certificates",
                "retention_period_days": 3650,  # 10 years for certificates
                "anonymization_required": False,
                "deletion_method": "soft",
                "description": "Certificate retention for security and compliance",
            },
        ]

        for policy in policies:
            await self.create_data_retention_policy(**policy)

    async def record_consent(
        self,
        customer_identifier: str,
        consent_type: str,
        consent_status: str = "granted",
        consent_method: str = "explicit",
        consent_source: str = "web_portal",
        legal_basis: str = "consent",
        expiry_date: Optional[datetime] = None,
    ) -> str:
        """Record customer consent."""
        consent_id = str(uuid.uuid4())

        consent_data = {
            "consent_id": consent_id,
            "customer_identifier": customer_identifier,
            "consent_type": consent_type,
            "consent_status": consent_status,
            "consent_date": datetime.now(timezone.utc),
            "consent_method": consent_method,
            "consent_source": consent_source,
            "legal_basis": legal_basis,
            "expiry_date": expiry_date,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }

        await self.timescale_client.store_consent_record(consent_data)
        self.logger.info(f"Recorded consent {consent_id} for customer {customer_identifier}")

        return consent_id

    async def revoke_consent(self, customer_identifier: str, consent_type: str) -> None:
        """Revoke customer consent."""
        consent_records = await self.timescale_client.get_consent_records(
            customer_identifier, consent_type
        )

        for record in consent_records:
            if record["consent_status"] == "granted":
                # Update consent record
                consent_data = {
                    "consent_id": record["consent_id"],
                    "customer_identifier": customer_identifier,
                    "consent_type": consent_type,
                    "consent_status": "revoked",
                    "consent_date": record["consent_date"],
                    "revocation_date": datetime.now(timezone.utc),
                    "consent_method": record["consent_method"],
                    "consent_source": record["consent_source"],
                    "legal_basis": record["legal_basis"],
                    "expiry_date": record["expiry_date"],
                    "created_at": record["created_at"],
                    "updated_at": datetime.now(timezone.utc),
                }

                await self.timescale_client.store_consent_record(consent_data)
                self.logger.info(
                    f"Revoked consent for customer {customer_identifier}, type {consent_type}"
                )

    async def anonymize_customer_data(self, customer_identifier: str, data_type: str) -> None:
        """Anonymize customer data."""
        try:
            await self.timescale_client.anonymize_customer_data(customer_identifier, data_type)
            self.logger.info(f"Anonymized {data_type} data for customer {customer_identifier}")
        except Exception as e:
            self.logger.error(f"Error anonymizing customer data: {e}")
            raise

    async def process_data_subject_request(
        self,
        customer_identifier: str,
        request_type: str,
        verification_method: str = "email",
        request_details: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Process data subject rights request."""
        request_id = str(uuid.uuid4())

        request_data = {
            "request_id": request_id,
            "customer_identifier": customer_identifier,
            "request_type": request_type,
            "request_status": "pending",
            "request_date": datetime.now(timezone.utc),
            "verification_method": verification_method,
            "verification_status": "pending",
            "request_details": request_details or {},
            "response_data": {},
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }

        await self.timescale_client.store_data_subject_request(request_data)

        # Process request based on type
        if request_type == "access":
            await self._process_access_request(request_id, customer_identifier)
        elif request_type == "erasure":
            await self._process_erasure_request(request_id, customer_identifier)
        elif request_type == "portability":
            await self._process_portability_request(request_id, customer_identifier)

        self.logger.info(
            f"Processed {request_type} request {request_id} for customer {customer_identifier}"
        )
        return request_id

    async def cleanup_expired_data(self) -> None:
        """Clean up expired data based on retention policies."""
        try:
            policies = await self.timescale_client.get_data_retention_policies()
            current_time = datetime.now(timezone.utc)

            for policy in policies:
                cutoff_date = current_time - timedelta(days=policy["retention_period_days"])

                if policy["data_type"] == "transaction_data":
                    await self._cleanup_transaction_data(cutoff_date, policy)
                elif policy["data_type"] == "meter_values":
                    await self._cleanup_meter_values(cutoff_date, policy)
                elif policy["data_type"] == "logs":
                    await self._cleanup_logs(cutoff_date, policy)

            self.logger.info("Completed data cleanup based on retention policies")

        except Exception as e:
            self.logger.error(f"Error during data cleanup: {e}")

    async def _process_customer_information_request(
        self,
        station_id: str,
        customer_certificate_id: Optional[str],
        id_token: Optional[IdTokenType],
        customer_identifier: Optional[str],
    ) -> Dict[str, Any]:
        """Process customer information request."""
        # This would typically involve:
        # 1. Verifying customer identity
        # 2. Retrieving customer data from various sources
        # 3. Applying privacy filters
        # 4. Returning anonymized/pseudonymized data

        customer_info = {
            "customer_identifier": customer_identifier or "ANONYMIZED",
            "id_token": id_token,
            "customer_certificate_id": customer_certificate_id,
        }

        return customer_info

    async def _process_delete_customer_information_request(
        self,
        station_id: str,
        customer_certificate_id: Optional[str],
        id_token: Optional[IdTokenType],
        customer_identifier: Optional[str],
    ) -> None:
        """Process delete customer information request."""
        # This would typically involve:
        # 1. Verifying customer identity
        # 2. Checking legal obligations (e.g., financial records)
        # 3. Anonymizing or deleting data based on retention policies
        # 4. Logging the deletion for audit purposes

        if customer_identifier:
            # Anonymize transaction data
            await self.anonymize_customer_data(customer_identifier, "transaction_data")

            # Revoke all consents
            for consent_type in [ct.value for ct in ConsentType]:
                await self.revoke_consent(customer_identifier, consent_type)

    async def _process_access_request(self, request_id: str, customer_identifier: str) -> None:
        """Process data access request."""
        # Collect all customer data
        customer_data = {
            "transactions": await self._get_customer_transactions(customer_identifier),
            "consent_records": await self.timescale_client.get_consent_records(customer_identifier),
            "anonymization_log": await self.timescale_client.get_pii_anonymization_log(
                customer_identifier
            ),
        }

        # Update request with response data
        await self._update_data_subject_request(request_id, "completed", customer_data)

    async def _process_erasure_request(self, request_id: str, customer_identifier: str) -> None:
        """Process data erasure request."""
        # Check for legal obligations that prevent erasure
        legal_obligations = await self._check_legal_obligations(customer_identifier)

        if legal_obligations:
            await self._update_data_subject_request(
                request_id,
                "rejected",
                {"reason": "Legal obligations prevent erasure", "obligations": legal_obligations},
            )
        else:
            # Anonymize all customer data
            data_types = ["transaction_data", "meter_values", "logs"]
            for data_type in data_types:
                await self.anonymize_customer_data(customer_identifier, data_type)

            await self._update_data_subject_request(
                request_id, "completed", {"message": "Customer data has been anonymized"}
            )

    async def _process_portability_request(self, request_id: str, customer_identifier: str) -> None:
        """Process data portability request."""
        # Collect portable customer data
        portable_data = {
            "transactions": await self._get_customer_transactions(customer_identifier),
            "consent_records": await self.timescale_client.get_consent_records(customer_identifier),
        }

        await self._update_data_subject_request(request_id, "completed", portable_data)

    async def _update_data_subject_request(
        self, request_id: str, status: str, response_data: Dict[str, Any]
    ) -> None:
        """Update data subject request."""
        # This would update the request in the database
        # Implementation depends on the specific database method
        pass

    async def _get_customer_transactions(self, customer_identifier: str) -> List[Dict[str, Any]]:
        """Get customer transactions."""
        # This would retrieve customer transactions from the database
        # Implementation depends on the specific database method
        return []

    async def _check_legal_obligations(self, customer_identifier: str) -> List[str]:
        """Check for legal obligations that prevent data erasure."""
        # This would check for legal obligations like:
        # - Financial record retention requirements
        # - Tax compliance obligations
        # - Regulatory reporting requirements
        return []

    async def _cleanup_transaction_data(
        self, cutoff_date: datetime, policy: Dict[str, Any]
    ) -> None:
        """Clean up old transaction data."""
        # Implementation would depend on specific database schema
        pass

    async def _cleanup_meter_values(self, cutoff_date: datetime, policy: Dict[str, Any]) -> None:
        """Clean up old meter values."""
        # Implementation would depend on specific database schema
        pass

    async def _cleanup_logs(self, cutoff_date: datetime, policy: Dict[str, Any]) -> None:
        """Clean up old logs."""
        # Implementation would depend on specific database schema
        pass

    def _hash_pii(self, value: str) -> str:
        """Hash PII for anonymization."""
        return hashlib.sha256(value.encode()).hexdigest()

    def _mask_pii(self, value: str, mask_char: str = "*") -> str:
        """Mask PII for anonymization."""
        if len(value) <= 4:
            return mask_char * len(value)
        return value[:2] + mask_char * (len(value) - 4) + value[-2:]
