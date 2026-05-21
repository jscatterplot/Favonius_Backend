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
        """Fail-closed: unconfigured deploy never leaks Prometheus data."""
        with patch("src.api.main._METRICS_TOKEN", ""):
            response = _client().get("/metrics")
        assert response.status_code == 503
        assert "METRICS_TOKEN" in response.json()["detail"]

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
