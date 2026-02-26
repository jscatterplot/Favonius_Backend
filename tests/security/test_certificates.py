"""Test certificate generation utilities."""

import base64
from datetime import datetime, timedelta

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def generate_test_certificate(common_name: str = "Test Station", valid_days: int = 365) -> str:
    """Generate a valid test certificate."""
    # Generate private key
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    # Generate certificate
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
            x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "CA"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Favonius Test"),
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
        .not_valid_after(datetime.utcnow() + timedelta(days=valid_days))
        .sign(private_key, hashes.SHA256())
    )

    # Convert to PEM format
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)

    # Convert PEM to base64 for OCPP certificate format
    cert_base64 = base64.b64encode(cert_pem).decode("utf-8")
    return cert_base64


def generate_expired_certificate() -> str:
    """Generate an expired test certificate."""
    # Generate private key
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    # Generate certificate
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
            x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "CA"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Favonius Test"),
            x509.NameAttribute(NameOID.COMMON_NAME, "Expired Test Station"),
        ]
    )

    # Create expired certificate (valid in the past)
    now = datetime.utcnow()
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=400))  # Started 400 days ago
        .not_valid_after(now - timedelta(days=30))  # Expired 30 days ago
        .sign(private_key, hashes.SHA256())
    )

    # Convert to PEM format
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)

    # Convert PEM to base64 for OCPP certificate format
    cert_base64 = base64.b64encode(cert_pem).decode("utf-8")
    return cert_base64


def generate_invalid_format_certificate() -> str:
    """Generate an invalid format certificate (not PEM)."""
    return "INVALID_CERT_FORMAT_NOT_PEM"


def generate_invalid_base64_certificate() -> str:
    """Generate an invalid base64 certificate."""
    return "-----BEGIN CERTIFICATE-----\nINVALID_BASE64_DATA\n-----END CERTIFICATE-----"


# Pre-generated certificates for testing
VALID_TEST_CERTIFICATE = generate_test_certificate("Valid Test Station")
EXPIRED_TEST_CERTIFICATE = generate_expired_certificate()
INVALID_FORMAT_CERTIFICATE = generate_invalid_format_certificate()
INVALID_BASE64_CERTIFICATE = generate_invalid_base64_certificate()
