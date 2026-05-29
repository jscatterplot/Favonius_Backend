"""Unit tests for log-line PII/secret redaction."""

from src.observability.log_buffer import redact_log_line


def test_redacts_email():
    out = redact_log_line("login from joris@example.com ok")
    assert "joris@example.com" not in out
    assert "[REDACTED_EMAIL]" in out


def test_redacts_jwt():
    jwt = (
        "eyJhbGciOiJIUzI1NiJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    )
    out = redact_log_line(f"auth token {jwt} accepted")
    assert jwt not in out
    assert "[REDACTED_JWT]" in out


def test_redacts_bearer_token():
    out = redact_log_line("Authorization: Bearer supersecrettoken12345")
    assert "supersecrettoken12345" not in out
    assert "[REDACTED]" in out


def test_redacts_key_value_secret():
    out = redact_log_line("password=hunter2 and api_key: sk-abcdef123456")
    assert "hunter2" not in out
    assert "sk-abcdef123456" not in out
    assert "[REDACTED]" in out


def test_redacts_long_hex_blob():
    secret = "a1b2c3d4" * 5  # 40 hex chars
    out = redact_log_line(f"signing key {secret} loaded")
    assert secret not in out
    assert "[REDACTED_HEX]" in out


def test_benign_line_untouched():
    line = "optimization run completed in 12.3s for depot abc"
    assert redact_log_line(line) == line


def test_empty_and_none_return_empty_string():
    assert redact_log_line("") == ""
    assert redact_log_line(None) == ""


def test_idempotent():
    text = "user a@b.com password=secret123 hash a1b2c3d4a1b2c3d4a1b2c3d4a1b2c3d4ab"
    once = redact_log_line(text)
    twice = redact_log_line(once)
    assert once == twice
    assert "a@b.com" not in once and "secret123" not in once


def test_redacts_json_quoted_secret_key():
    out = redact_log_line('{"password": "hunter2", "user": "bob"}')
    assert "hunter2" not in out
    assert "bob" in out  # non-secret value preserved


def test_redacts_python_dict_repr_secret():
    out = redact_log_line("auth payload {'password': 'hunter2'}")
    assert "hunter2" not in out


def test_redacts_space_bearing_value():
    out = redact_log_line("password = my secret phrase here")
    assert "secret phrase here" not in out  # the whole value, not just the first word
    assert "[REDACTED]" in out


def test_redacts_connection_string_password():
    # Password is always removed; a non-FQDN host (no email-shaped @host.tld)
    # is preserved. (FQDN hosts may be over-redacted by the email pass — safe.)
    out = redact_log_line("dsn postgres://svcuser:p4ssw0rd@localhost:5432/app")
    assert "p4ssw0rd" not in out
    assert "svcuser" in out and "localhost" in out


def test_redacts_prefixed_api_key():
    out = redact_log_line("loaded key sk-proj-AbCdEf0123456789Xyz")
    assert "sk-proj-AbCdEf0123456789Xyz" not in out
    assert "[REDACTED_KEY]" in out


def test_bearer_header_not_double_mangled_and_secret_free():
    out = redact_log_line("Authorization: Bearer supersecrettoken12345")
    assert "supersecrettoken12345" not in out
    assert "[REDACTED] [REDACTED]" not in out  # single, clean redaction


def test_preserves_non_secret_fields_after_value():
    # The greedy unquoted value must stop at the next field, not eat the line.
    out = redact_log_line("password=hunter2 http_status=200 duration=5ms")
    assert "hunter2" not in out
    assert "http_status=200" in out
    assert "duration=5ms" in out
