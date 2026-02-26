"""Unit tests for ControllerConfig.

Reference: PRD.md#11-2-unit-test-requirements
"""

import os
from unittest.mock import patch

import pytest

from src.core.controller_config import ControllerConfig


class TestControllerConfigDefaults:
    """Test ControllerConfig default values."""

    def test_optimization_timeout_default(self):
        """Test optimization_timeout default is 60.0 seconds (not 30.0).

        Per PRD Section 8.2, optimization timeout should be 60 seconds.
        """
        config = ControllerConfig()
        assert (
            config.optimization_timeout == 60.0
        ), "Default optimization_timeout should be 60.0 seconds per PRD Section 8.2"

    def test_trigger_cooldown_minutes_default(self):
        """Test trigger_cooldown_minutes default."""
        config = ControllerConfig()
        assert config.trigger_cooldown_minutes == 5, "Default trigger_cooldown_minutes should be 5"


class TestControllerConfigFromEnv:
    """Test ControllerConfig.from_env() method."""

    def test_from_env_optimization_timeout_default(self):
        """Test from_env() default optimization_timeout is '60.0' (not '30.0')."""
        with patch.dict(os.environ, {}, clear=True):
            config = ControllerConfig.from_env()
            assert (
                config.optimization_timeout == 60.0
            ), "from_env() default optimization_timeout should be 60.0 seconds"

    def test_from_env_optimization_timeout_override(self):
        """Test FAVONIUS_OPTIMIZATION_TIMEOUT environment variable works."""
        with patch.dict(os.environ, {"FAVONIUS_OPTIMIZATION_TIMEOUT": "45.0"}):
            config = ControllerConfig.from_env()
            assert config.optimization_timeout == 45.0

    def test_from_env_optimization_timeout_invalid(self):
        """Test invalid timeout values are handled."""
        with patch.dict(os.environ, {"FAVONIUS_OPTIMIZATION_TIMEOUT": "invalid"}):
            with pytest.raises((ValueError, TypeError)):
                ControllerConfig.from_env()

    def test_from_env_trigger_cooldown_override(self):
        """Test FAVONIUS_TRIGGER_COOLDOWN_MINUTES environment variable works."""
        with patch.dict(os.environ, {"FAVONIUS_TRIGGER_COOLDOWN_MINUTES": "10"}):
            config = ControllerConfig.from_env()
            assert config.trigger_cooldown_minutes == 10

    def test_from_env_all_parameters(self):
        """Test from_env() with all environment variables."""
        env_vars = {
            "FAVONIUS_OPTIMIZATION_TIMEOUT": "90.0",
            "FAVONIUS_TRIGGER_COOLDOWN_MINUTES": "7",
            "FAVONIUS_OPTIMIZATION_HORIZON_HOURS": "48",
        }
        with patch.dict(os.environ, env_vars):
            config = ControllerConfig.from_env()
            assert config.optimization_timeout == 90.0
            assert config.trigger_cooldown_minutes == 7
            assert config.optimization_horizon_hours == 48
