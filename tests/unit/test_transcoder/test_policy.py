"""Tests for TranscoderPolicy configuration model."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from eggpool.transcoder.policy import TranscoderPolicy

if TYPE_CHECKING:
    from pathlib import Path


def test_defaults() -> None:
    policy = TranscoderPolicy()
    # Translation is on by default; the `enabled` flag is a deprecated
    # escape hatch.
    assert policy.enabled is True
    assert policy.loss_policy == "warn"
    assert policy.prefer_native is True


def test_extra_forbid() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TranscoderPolicy(extra_field="nope")  # type: ignore[arg-type]


def test_custom_values() -> None:
    policy = TranscoderPolicy(enabled=True, loss_policy="reject", prefer_native=False)
    assert policy.enabled is True
    assert policy.loss_policy == "reject"
    assert policy.prefer_native is False


def test_round_trip_toml(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[server]
host = "127.0.0.1"
port = 9000
api_key_env = "TEST_RT_KEY"

[transcoder]
enabled = true
loss_policy = "reject"
prefer_native = false
"""
    )
    import os

    os.environ["TEST_RT_KEY"] = "key"
    try:
        from eggpool.models.config import AppConfig

        config = AppConfig.from_toml(str(config_file))
        assert config.transcoder.enabled is True
        assert config.transcoder.loss_policy == "reject"
        assert config.transcoder.prefer_native is False
    finally:
        del os.environ["TEST_RT_KEY"]


def test_default_config_has_transcoder() -> None:
    from eggpool.models.config import AppConfig

    config = AppConfig()
    # Translation is on by default; the `enabled` flag is a deprecated
    # escape hatch.
    assert config.transcoder.enabled is True
    assert config.transcoder.loss_policy == "warn"
    assert config.transcoder.prefer_native is True


def test_explicit_disabled_escape_hatch() -> None:
    """Setting enabled=False reverts to legacy protocol-exact routing."""
    policy = TranscoderPolicy(enabled=False)
    assert policy.enabled is False
