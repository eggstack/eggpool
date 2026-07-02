"""Tests for CompressionConfig (Phase 4)."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

if TYPE_CHECKING:
    from pathlib import Path

from eggpool.transcoder.compression.policy import (
    CompressionConfig,
    CompressionTransforms,
)


def test_defaults() -> None:
    policy = CompressionConfig()
    assert policy.enabled is False
    assert policy.mode == "observe"
    assert policy.placement == "suffix_only"
    assert policy.respect_cache_boundaries is True
    assert policy.compress_static_prefix is False
    assert policy.min_candidate_tokens == 2048
    assert policy.min_savings_tokens == 1024
    assert policy.max_compression_latency_ms == 25.0
    assert policy.transforms.fold_repeated_lines is True
    assert policy.transforms.compact_logs is True
    assert policy.transforms.compact_search_results is True
    assert policy.transforms.elide_base64_blobs is True
    assert policy.transforms.minify_machine_json is True
    assert policy.transforms.compact_stack_traces is True


def test_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        CompressionConfig(extra_field="nope")  # type: ignore[arg-type]


def test_unknown_mode_fails() -> None:
    with pytest.raises(ValidationError):
        CompressionConfig(enabled=True, mode="safe")  # type: ignore[arg-type]


def test_compress_static_prefix_blocked_in_observe() -> None:
    with pytest.raises(ValidationError) as excinfo:
        CompressionConfig(enabled=True, mode="observe", compress_static_prefix=True)
    assert "compress_static_prefix" in str(excinfo.value).lower()


def test_non_negative_thresholds() -> None:
    with pytest.raises(ValidationError):
        CompressionConfig(min_candidate_tokens=-1)
    with pytest.raises(ValidationError):
        CompressionConfig(min_savings_tokens=-1)
    with pytest.raises(ValidationError):
        CompressionConfig(max_compression_latency_ms=-0.1)


def test_transform_toggle_overrides() -> None:
    transforms = CompressionTransforms(
        fold_repeated_lines=False,
        compact_logs=True,
        compact_search_results=False,
        elide_base64_blobs=True,
        minify_machine_json=False,
        compact_stack_traces=True,
    )
    policy = CompressionConfig(enabled=True, transforms=transforms)
    assert policy.transforms.fold_repeated_lines is False
    assert policy.transforms.compact_search_results is False


def test_transforms_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        CompressionTransforms(unknown=True)  # type: ignore[arg-type]


def test_round_trip_toml(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[server]
host = "127.0.0.1"
port = 9000
api_key_env = "TEST_RT_KEY"

[compression]
enabled = true
mode = "observe"
placement = "suffix_only"
respect_cache_boundaries = true
compress_static_prefix = false
min_candidate_tokens = 4096
min_savings_tokens = 2048
max_compression_latency_ms = 50.0

[compression.transforms]
fold_repeated_lines = true
compact_logs = false
compact_search_results = true
elide_base64_blobs = true
minify_machine_json = true
compact_stack_traces = true
"""
    )
    os.environ["TEST_RT_KEY"] = "key"
    try:
        from eggpool.models.config import AppConfig

        config = AppConfig.from_toml(str(config_file))
        assert config.compression.enabled is True
        assert config.compression.mode == "observe"
        assert config.compression.placement == "suffix_only"
        assert config.compression.respect_cache_boundaries is True
        assert config.compression.min_candidate_tokens == 4096
        assert config.compression.min_savings_tokens == 2048
        assert config.compression.max_compression_latency_ms == 50.0
        assert config.compression.transforms.compact_logs is False
    finally:
        del os.environ["TEST_RT_KEY"]


def test_default_config_has_compression() -> None:
    from eggpool.models.config import AppConfig

    config = AppConfig()
    assert config.compression.enabled is False
    assert config.compression.mode == "observe"


def test_appconfig_rejects_unknown_top_level_compression_field() -> None:
    """Unknown top-level keys must fail closed (extra=forbid)."""
    from eggpool.models.config import AppConfig

    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "compression": {
                    "enabled": True,
                    "not_a_real_field": True,
                }
            }
        )


def test_compression_policy_dict_round_trip() -> None:
    """A dict payload round-trips through ``AppConfig.from_dict``."""
    from eggpool.models.config import AppConfig

    payload: dict[str, Any] = {
        "compression": {
            "enabled": True,
            "mode": "observe",
            "min_savings_tokens": 0,
        }
    }
    config = AppConfig.from_dict(payload)
    assert config.compression.enabled is True
    assert config.compression.min_savings_tokens == 0
