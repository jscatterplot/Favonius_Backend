"""Tests for the auth gate on ``GET /metrics``.

The endpoint exposes per-depot operational counters that leak business
activity, so it must refuse every request when ``METRICS_TOKEN`` is
unset and only honour requests carrying the matching bearer.
"""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient


def _client() -> TestClient:
    from src.api.main import app

    return TestClient(app)


class TestMetricsAuth:
    def test_503_when_token_unset(self):
        """Fail-closed: unconfigured deploy never leaks Prometheus data.

        The API's global ``http_exception_handler`` sanitises the detail
        string for any 5xx response, so we assert on the stable
        ``error_code`` envelope field rather than the detail content.
        """
        with patch("src.api.main._METRICS_TOKEN", ""):
            response = _client().get("/metrics")
        assert response.status_code == 503
        payload = response.json()
        assert payload["error_code"] == "SERVICE_UNAVAILABLE"
        # Regression guard: secret-bearing details must never leak through
        # 5xx responses, even when the underlying exception mentions the env var.
        assert "METRICS_TOKEN" not in payload.get("detail", "")

    def test_401_when_authorization_missing(self):
        with patch("src.api.main._METRICS_TOKEN", "test-token"):
            response = _client().get("/metrics")
        assert response.status_code == 401

    def test_401_when_authorization_wrong_value(self):
        with patch("src.api.main._METRICS_TOKEN", "test-token"):
            response = _client().get(
                "/metrics", headers={"Authorization": "Bearer not-the-token"}
            )
        assert response.status_code == 401

    def test_401_when_authorization_missing_bearer_prefix(self):
        with patch("src.api.main._METRICS_TOKEN", "test-token"):
            response = _client().get(
                "/metrics", headers={"Authorization": "test-token"}
            )
        assert response.status_code == 401

    def test_401_when_authorization_uses_basic_scheme(self):
        """Scheme is matched case-insensitively but only ``bearer`` is accepted."""
        with patch("src.api.main._METRICS_TOKEN", "test-token"):
            response = _client().get(
                "/metrics", headers={"Authorization": "Basic dGVzdC10b2tlbg=="}
            )
        assert response.status_code == 401

    def test_200_with_matching_bearer(self):
        with patch("src.api.main._METRICS_TOKEN", "test-token"):
            response = _client().get(
                "/metrics", headers={"Authorization": "Bearer test-token"}
            )
        assert response.status_code == 200
        # Prometheus text exposition format. Body may be empty in a stripped
        # registry — content-type is the stable signal.
        assert response.headers["content-type"].startswith("text/plain")

    def test_200_with_lowercase_bearer_scheme(self):
        """RFC 7235: HTTP auth scheme is case-insensitive."""
        with patch("src.api.main._METRICS_TOKEN", "test-token"):
            response = _client().get(
                "/metrics", headers={"Authorization": "bearer test-token"}
            )
        assert response.status_code == 200

    def test_200_with_mixed_case_bearer_scheme(self):
        with patch("src.api.main._METRICS_TOKEN", "test-token"):
            response = _client().get(
                "/metrics", headers={"Authorization": "BeArEr test-token"}
            )
        assert response.status_code == 200

    def test_401_when_token_case_differs(self):
        """The token itself (the secret) remains case-sensitive."""
        with patch("src.api.main._METRICS_TOKEN", "test-token"):
            response = _client().get(
                "/metrics", headers={"Authorization": "Bearer Test-Token"}
            )
        assert response.status_code == 401

    def test_bearer_token_with_special_chars_does_not_match_substring(self):
        """Catch any future regression where a substring compare slips in."""
        with patch("src.api.main._METRICS_TOKEN", "abc"):
            response = _client().get(
                "/metrics", headers={"Authorization": "Bearer abcd"}
            )
        assert response.status_code == 401

    def test_200_with_multi_space_bearer(self):
        """RFC 7235 allows ``1*SP`` between scheme and token."""
        with patch("src.api.main._METRICS_TOKEN", "test-token"):
            response = _client().get(
                "/metrics", headers={"Authorization": "Bearer   test-token"}
            )
        assert response.status_code == 200

    def test_401_when_bearer_contains_non_ascii(self):
        """Non-ASCII bearer must return 401, not crash to 500.

        ``secrets.compare_digest`` raises ``TypeError`` when given a
        non-ASCII ``str``; without the byte-encoding fix, a request
        carrying obs-text in the credential would surface as an
        unhandled exception (500) and create a noisy availability
        signal. The handler must treat it as plain unauthorised.
        """
        with patch("src.api.main._METRICS_TOKEN", "test-token"):
            response = _client().get(
                "/metrics", headers={"Authorization": "Bearer wörld"}
            )
        assert response.status_code == 401
