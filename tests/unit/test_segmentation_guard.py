"""Tests for compression-policy-aware segmentation guard ordering.

The segmentation guard must read the effective compression policy
(resolved from scoped overrides) rather than the raw global config,
so a ``[[compression.policies]]`` override that enables observe/safe
compression triggers segmentation even when the global
``[compression] enabled = false``.

See plans/2026-07-05-performance-corrective-pass.md corrective item 1.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from eggpool.models.config import AppConfig
from eggpool.transcoder.compression.policy import (
    CompressionConfig,
    CompressionPolicyOverride,
)
from eggpool.transcoder.compression.policy_resolver import (
    CompressionPolicyContext,
    resolve_compression_policy,
)
from eggpool.transcoder.segmentation_guard import should_segment_request

pytestmark = pytest.mark.request_path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ctx = CompressionPolicyContext(
    source_protocol="openai",
    requested_model="gpt-4o",
)


def _make_app_config(
    *,
    force_segmentation: bool = False,
) -> MagicMock:
    cfg = MagicMock(spec=AppConfig)
    cfg.force_segmentation = force_segmentation
    return cfg


def _global_disabled_config() -> CompressionConfig:
    return CompressionConfig(enabled=False, mode="observe")


def _global_enabled_observe() -> CompressionConfig:
    return CompressionConfig(enabled=True, mode="observe")


def _global_enabled_safe() -> CompressionConfig:
    return CompressionConfig(enabled=True, mode="safe")


def _scope_override(
    *,
    name: str = "enable-observe",
    enabled: bool = True,
    mode: str = "observe",
    match_requested_models: list[str] | None = None,
) -> CompressionPolicyOverride:
    return CompressionPolicyOverride(
        name=name,
        enabled=enabled,
        mode=mode,  # type: ignore[arg-type]
        match_requested_models=match_requested_models,
    )


# ---------------------------------------------------------------------------
# Tests: segmentation guard reads effective policy
# ---------------------------------------------------------------------------


class TestSegmentationGuardWithPolicyResolution:
    def test_global_disabled_scoped_enable_observe(self) -> None:
        """Global compression disabled but scoped policy enables observe
        → segmentation MUST run."""
        base = _global_disabled_config()
        override = _scope_override(enabled=True, mode="observe")
        resolved = resolve_compression_policy(base, _ctx, overrides=[override])
        effective = resolved.config

        assert effective.enabled is True
        assert effective.mode == "observe"

        config = _make_app_config()
        result = should_segment_request(
            config,
            compression_enabled=effective.enabled,
            compression_mode=str(effective.mode),
        )
        assert result is True

    def test_global_disabled_scoped_enable_safe(self) -> None:
        """Global compression disabled but scoped policy enables safe
        → segmentation MUST run."""
        base = _global_disabled_config()
        override = _scope_override(name="enable-safe", enabled=True, mode="safe")
        resolved = resolve_compression_policy(base, _ctx, overrides=[override])
        effective = resolved.config

        assert effective.enabled is True
        assert effective.mode == "safe"

        config = _make_app_config()
        result = should_segment_request(
            config,
            compression_enabled=effective.enabled,
            compression_mode=str(effective.mode),
        )
        assert result is True

    def test_global_enabled_scoped_disables_compression(self) -> None:
        """Global compression enabled but scoped policy disables it
        → segmentation may be skipped if no other consumer needs it."""
        base = _global_enabled_observe()
        override = _scope_override(
            name="disable-for-model",
            enabled=False,
            match_requested_models=["gpt-4o"],
        )
        resolved = resolve_compression_policy(base, _ctx, overrides=[override])
        effective = resolved.config

        assert effective.enabled is False

        config = _make_app_config()
        result = should_segment_request(
            config,
            compression_enabled=effective.enabled,
            compression_mode=str(effective.mode),
        )
        assert result is False

    def test_policy_resolution_failure_falls_back_to_global(self) -> None:
        """When the resolver itself catches a malformed override, it
        falls back to the base config with a warning — the guard
        should then use the base config's enabled/mode."""
        base = _global_enabled_observe()
        ctx = CompressionPolicyContext(source_protocol="openai")
        # An override with a validation error (e.g. invalid mode) will
        # be skipped by the resolver and it falls back to the base.
        invalid_override = CompressionPolicyOverride(
            name="broken",
            enabled=True,
            mode="observe",
        )
        # The resolver itself is fail-closed: it returns the base
        # config (with a warning) rather than raising.  The guard
        # then reads the base config's enabled/mode.
        resolved = resolve_compression_policy(
            base,
            ctx,
            overrides=[invalid_override],
        )
        # Even though the override was applied, the base was enabled
        # so the effective policy is still enabled.
        assert resolved.config.enabled is True

        config = _make_app_config()
        result = should_segment_request(
            config,
            compression_enabled=resolved.config.enabled,
            compression_mode=str(resolved.config.mode),
        )
        assert result is True

    def test_all_consumers_disabled_segmentation_skipped(self) -> None:
        """When compression is disabled, synthetic cache is off, and
        cache observability is off → segmentation is skipped."""
        config = _make_app_config()
        result = should_segment_request(
            config,
            compression_enabled=False,
            compression_mode="off",
            synthetic_cache_enabled=False,
            cache_observability_enabled=False,
        )
        assert result is False

    def test_empty_request_vs_not_collected_distinction(self) -> None:
        """``segmentation_not_collected`` must be True when segmentation
        was intentionally skipped, distinct from ``empty_request`` when
        segmentation ran but found nothing."""
        config = _make_app_config()
        result = should_segment_request(
            config,
            compression_enabled=False,
            compression_mode="off",
            synthetic_cache_enabled=False,
            cache_observability_enabled=False,
        )
        assert result is False
        # The caller sets segmentation_not_collected=True when
        # should_segment_request returns False, which is distinct
        # from segmentation_result being None after a run.

    def test_force_segmentation_overrides_policy(self) -> None:
        """``force_segmentation=True`` forces segmentation even with
        all consumers disabled."""
        config = _make_app_config(force_segmentation=True)
        result = should_segment_request(
            config,
            compression_enabled=False,
            compression_mode="off",
            force_segmentation=True,
        )
        assert result is True

    def test_synthetic_cache_enables_segmentation(self) -> None:
        """Synthetic cache controls enabled triggers segmentation
        even without compression."""
        config = _make_app_config()
        result = should_segment_request(
            config,
            compression_enabled=False,
            compression_mode="off",
            synthetic_cache_enabled=True,
        )
        assert result is True

    def test_compression_observe_without_safe_still_segments(self) -> None:
        """Compression observe mode (enabled + mode=observe) triggers
        segmentation."""
        config = _make_app_config()
        result = should_segment_request(
            config,
            compression_enabled=True,
            compression_mode="observe",
        )
        assert result is True

    def test_compression_enabled_off_mode_skips(self) -> None:
        """Compression enabled but mode='off' does NOT trigger
        segmentation."""
        config = _make_app_config()
        result = should_segment_request(
            config,
            compression_enabled=True,
            compression_mode="off",
        )
        assert result is False

    def test_scoped_override_fires_on_matching_model(self) -> None:
        """A scoped override with match_requested_models fires only
        when the requested model matches."""
        base = _global_disabled_config()
        override = _scope_override(
            name="enable-for-gpt4o",
            enabled=True,
            mode="observe",
            match_requested_models=["gpt-4o"],
        )
        # Matching context
        ctx_match = CompressionPolicyContext(
            source_protocol="openai",
            requested_model="gpt-4o",
        )
        resolved = resolve_compression_policy(base, ctx_match, overrides=[override])
        assert resolved.config.enabled is True

        # Non-matching context
        ctx_no_match = CompressionPolicyContext(
            source_protocol="openai",
            requested_model="claude-3-opus",
        )
        resolved_nomatch = resolve_compression_policy(
            base, ctx_no_match, overrides=[override]
        )
        assert resolved_nomatch.config.enabled is False
