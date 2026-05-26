"""Reversible encryption for stored third-party credentials.

Data-source connections (the "Data Sources" page) persist a user's external
API credentials so the backend can re-connect on a schedule. Those secrets must
be *replayable*, so the one-way bcrypt hashing used for ``station_credentials``
does not apply — we use Fernet symmetric encryption instead.

The key is read from ``DATA_SOURCES_ENCRYPTION_KEY``:

* a single urlsafe-base64 Fernet key, e.g. ``abc123...=`` → version 1, or
* a rotation spec of ``version:key`` pairs, comma-separated, e.g.
  ``1:oldkey...=,2:newkey...=`` → decrypt with the version recorded on the row,
  encrypt with the highest version.

Generate a key::

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Plaintext never leaves this module except as the dict returned by
:func:`decrypt_credentials`; it is never logged.
"""

from __future__ import annotations

import json
import os
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

CURRENT_ENCRYPTION_VERSION = 1
_ENV_KEY = "DATA_SOURCES_ENCRYPTION_KEY"


class CredentialCipherError(RuntimeError):
    """Raised when encryption keys are missing/invalid or a payload won't decrypt."""


def _parse_key_spec(raw: str) -> dict[int, bytes]:
    """Parse ``DATA_SOURCES_ENCRYPTION_KEY`` into ``{version: key_bytes}``.

    Args:
        raw: The raw env-var value (single key or ``version:key`` rotation spec).

    Returns:
        Mapping of key version to the raw urlsafe-base64 Fernet key bytes.

    Raises:
        CredentialCipherError: If the spec is empty or malformed.
    """
    segments = [s.strip() for s in raw.split(",") if s.strip()]
    if not segments:
        raise CredentialCipherError(f"{_ENV_KEY} is empty")

    keys: dict[int, bytes] = {}
    for seg in segments:
        if ":" in seg:
            ver_str, _, key_str = seg.partition(":")
            try:
                version = int(ver_str)
            except ValueError as exc:
                raise CredentialCipherError(
                    f"{_ENV_KEY} rotation entry {seg.split(':')[0]!r} has a " "non-integer version"
                ) from exc
            keys[version] = key_str.strip().encode("utf-8")
        elif len(segments) > 1:
            raise CredentialCipherError(
                f"{_ENV_KEY} rotation spec needs 'version:key' for every entry"
            )
        else:
            keys[CURRENT_ENCRYPTION_VERSION] = seg.encode("utf-8")
    return keys


def _load_ciphers() -> tuple[dict[int, Fernet], int]:
    """Build ``{version: Fernet}`` from the environment plus the current version.

    Returns:
        A tuple of (version→Fernet map, highest version number).

    Raises:
        CredentialCipherError: If the env var is unset or any key is invalid.
    """
    raw = os.getenv(_ENV_KEY)
    if not raw:
        raise CredentialCipherError(f"{_ENV_KEY} is not set")

    ciphers: dict[int, Fernet] = {}
    for version, key_bytes in _parse_key_spec(raw).items():
        try:
            ciphers[version] = Fernet(key_bytes)
        except (ValueError, TypeError) as exc:
            raise CredentialCipherError(
                f"{_ENV_KEY} version {version} is not a valid Fernet key"
            ) from exc
    return ciphers, max(ciphers)


def is_configured() -> bool:
    """Return ``True`` if a usable encryption key is configured.

    Used by the feature-flag startup gate to fail closed when the Data Sources
    feature is enabled without a key.
    """
    try:
        _load_ciphers()
        return True
    except CredentialCipherError:
        return False


def encrypt_credentials(plaintext: dict[str, Any]) -> tuple[bytes, int]:
    """Encrypt a credentials dict for storage in ``encrypted_credentials BYTEA``.

    Args:
        plaintext: The credentials payload (e.g. ``{"provider_key": ...,
            "secrets": {...}, "schema_version": 1}``).

    Returns:
        A tuple of (Fernet token bytes, encryption version used).

    Raises:
        CredentialCipherError: If no key is configured.
    """
    ciphers, version = _load_ciphers()
    raw = json.dumps(plaintext, separators=(",", ":")).encode("utf-8")
    token = ciphers[version].encrypt(raw)
    return token, version


def decrypt_credentials(payload: bytes, version: int) -> dict[str, Any]:
    """Decrypt a stored credentials payload.

    Args:
        payload: The Fernet token bytes from ``encrypted_credentials``.
        version: The ``encryption_version`` recorded on the row.

    Returns:
        The original credentials dict.

    Raises:
        CredentialCipherError: If the version's key is missing or the token is
            invalid/corrupt (e.g. encrypted under a rotated-out key).
    """
    ciphers, _ = _load_ciphers()
    cipher = ciphers.get(version)
    if cipher is None:
        raise CredentialCipherError(
            f"no encryption key configured for version {version} " "(was the key rotated out?)"
        )
    try:
        raw = cipher.decrypt(bytes(payload))
    except InvalidToken as exc:
        raise CredentialCipherError(
            "credential payload failed to decrypt (wrong or rotated key)"
        ) from exc
    result: dict[str, Any] = json.loads(raw)
    return result
