"""Unit tests for the data-source credential cipher."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from src.security import credential_cipher as cc

pytestmark = pytest.mark.unit

_KEY_A = Fernet.generate_key().decode()
_KEY_B = Fernet.generate_key().decode()


def test_round_trip(monkeypatch):
    monkeypatch.setenv("DATA_SOURCES_ENCRYPTION_KEY", _KEY_A)
    payload = {"provider_key": "kempower", "secrets": {"username": "u", "password": "p"}}
    token, version = cc.encrypt_credentials(payload)
    assert version == 1
    assert isinstance(token, bytes)
    assert cc.decrypt_credentials(token, version) == payload


def test_wrong_key_fails(monkeypatch):
    monkeypatch.setenv("DATA_SOURCES_ENCRYPTION_KEY", _KEY_A)
    token, version = cc.encrypt_credentials({"secrets": {"password": "topsecret"}})
    monkeypatch.setenv("DATA_SOURCES_ENCRYPTION_KEY", _KEY_B)
    with pytest.raises(cc.CredentialCipherError) as exc:
        cc.decrypt_credentials(token, version)
    # The error must not leak the plaintext.
    assert "topsecret" not in str(exc.value)


def test_rotation_decrypts_old_and_new(monkeypatch):
    # Seal under v1.
    monkeypatch.setenv("DATA_SOURCES_ENCRYPTION_KEY", _KEY_A)
    old_token, old_version = cc.encrypt_credentials({"secrets": {"k": "v1"}})
    assert old_version == 1

    # Add v2 as the current key; v1 retained for decryption.
    monkeypatch.setenv("DATA_SOURCES_ENCRYPTION_KEY", f"1:{_KEY_A},2:{_KEY_B}")
    new_token, new_version = cc.encrypt_credentials({"secrets": {"k": "v2"}})
    assert new_version == 2
    assert cc.decrypt_credentials(old_token, 1) == {"secrets": {"k": "v1"}}
    assert cc.decrypt_credentials(new_token, 2) == {"secrets": {"k": "v2"}}


def test_missing_version_key(monkeypatch):
    monkeypatch.setenv("DATA_SOURCES_ENCRYPTION_KEY", _KEY_A)
    with pytest.raises(cc.CredentialCipherError):
        cc.decrypt_credentials(b"whatever", 99)


def test_not_configured(monkeypatch):
    monkeypatch.delenv("DATA_SOURCES_ENCRYPTION_KEY", raising=False)
    assert cc.is_configured() is False
    with pytest.raises(cc.CredentialCipherError):
        cc.encrypt_credentials({"secrets": {}})


def test_configured_true(monkeypatch):
    monkeypatch.setenv("DATA_SOURCES_ENCRYPTION_KEY", _KEY_A)
    assert cc.is_configured() is True


def test_malformed_rotation_spec(monkeypatch):
    monkeypatch.setenv("DATA_SOURCES_ENCRYPTION_KEY", f"{_KEY_A},{_KEY_B}")
    assert cc.is_configured() is False


def test_invalid_key_value(monkeypatch):
    monkeypatch.setenv("DATA_SOURCES_ENCRYPTION_KEY", "not-a-fernet-key")
    assert cc.is_configured() is False
