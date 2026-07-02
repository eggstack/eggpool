"""Tests for Phase 6 CompressionPolicyOverride and config policies."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eggpool.transcoder.compression.policy import (
    CompressionConfig,
    CompressionPolicyOverride,
)


class TestCompressionPolicyOverrideRoundTrip:
    """CompressionPolicyOverride round-trips through model_dump / model_validate."""

    def test_round_trip_basic(self) -> None:
        override = CompressionPolicyOverride(name="test", match_clients=["foo"])
        dumped = override.model_dump()
        restored = CompressionPolicyOverride.model_validate(dumped)
        assert restored.name == "test"
        assert restored.match_clients == ["foo"]

    def test_round_trip_preserves_all_fields(self) -> None:
        override = CompressionPolicyOverride(
            name="full",
            match_clients=["c1"],
            match_provider_ids=["p1"],
            match_provider_kinds=["pk1"],
            match_models=["m1"],
            match_requested_models=["rm1"],
            match_protocols=["openai"],
            match_transcoded=True,
            enabled=True,
            mode="safe",
            placement="suffix_only",
            respect_cache_boundaries=False,
            compress_static_prefix=False,
            min_candidate_tokens=512,
            min_savings_tokens=256,
            max_compression_latency_ms=10.0,
        )
        dumped = override.model_dump()
        restored = CompressionPolicyOverride.model_validate(dumped)
        assert restored.name == "full"
        assert restored.match_clients == ["c1"]
        assert restored.match_provider_ids == ["p1"]
        assert restored.match_provider_kinds == ["pk1"]
        assert restored.match_models == ["m1"]
        assert restored.match_requested_models == ["rm1"]
        assert restored.match_protocols == ["openai"]
        assert restored.match_transcoded is True
        assert restored.enabled is True
        assert restored.mode == "safe"
        assert restored.min_candidate_tokens == 512

    def test_has_any_match_field_true(self) -> None:
        override = CompressionPolicyOverride(name="test", match_clients=["a"])
        assert override.has_any_match_field() is True

    def test_has_any_match_field_false(self) -> None:
        override = CompressionPolicyOverride(name="test")
        assert override.has_any_match_field() is False


class TestCompressionConfigPolicies:
    """CompressionConfig with policies validation."""

    def test_default_policies_empty(self) -> None:
        config = CompressionConfig()
        assert config.policies == []

    def test_single_override_valid(self) -> None:
        override = CompressionPolicyOverride(name="my-policy", match_clients=["a"])
        config = CompressionConfig(policies=[override])
        assert len(config.policies) == 1
        assert config.policies[0].name == "my-policy"

    def test_duplicate_policy_names_rejected(self) -> None:
        o1 = CompressionPolicyOverride(name="x", match_clients=["a"])
        o2 = CompressionPolicyOverride(name="x", match_clients=["b"])
        with pytest.raises(ValidationError, match="duplicate name"):
            CompressionConfig(policies=[o1, o2])

    def test_catch_all_without_default_name_rejected(self) -> None:
        override = CompressionPolicyOverride(name="not-default")
        with pytest.raises(ValidationError, match="at least one match"):
            CompressionConfig(policies=[override])

    def test_catch_all_with_default_name_accepted(self) -> None:
        override = CompressionPolicyOverride(name="default")
        config = CompressionConfig(policies=[override])
        assert len(config.policies) == 1
        assert config.policies[0].name == "default"

    def test_match_clients_accepts_list(self) -> None:
        override = CompressionPolicyOverride(name="test", match_clients=["a", "b"])
        assert override.match_clients == ["a", "b"]

    def test_all_match_fields_default_to_none(self) -> None:
        override = CompressionPolicyOverride(name="test")
        assert override.match_clients is None
        assert override.match_provider_ids is None
        assert override.match_provider_kinds is None
        assert override.match_models is None
        assert override.match_requested_models is None
        assert override.match_protocols is None
        assert override.match_transcoded is None

    def test_match_clients_rejects_non_list_string(self) -> None:
        with pytest.raises(ValidationError):
            CompressionPolicyOverride(
                name="test",
                match_clients="not-a-list",  # type: ignore[arg-type]
            )

    def test_match_provider_ids_rejects_non_list(self) -> None:
        with pytest.raises(ValidationError):
            CompressionPolicyOverride(
                name="test",
                match_provider_ids="single",  # type: ignore[arg-type]
            )

    def test_compress_static_prefix_rejected_in_observe_override(
        self,
    ) -> None:
        """compress_static_prefix=True with mode='observe' is rejected
        at the override model level."""
        with pytest.raises(ValidationError, match="compress_static_prefix"):
            CompressionPolicyOverride(
                name="test",
                match_clients=["a"],
                compress_static_prefix=True,
                mode="observe",
            )

    def test_compress_static_prefix_rejected_in_safe_override(self) -> None:
        """compress_static_prefix=True with mode='safe' is rejected
        at the override model level."""
        with pytest.raises(ValidationError, match="compress_static_prefix"):
            CompressionPolicyOverride(
                name="test",
                match_clients=["a"],
                compress_static_prefix=True,
                mode="safe",
            )

    def test_compress_static_prefix_accepted_without_mode(self) -> None:
        """compress_static_prefix=True with no mode passes the override
        validator (mode is None).  BUG: overlay validation catches the
        mismatch with the base config at resolve time, but only when
        the overlay function does not reject extra fields first."""
        override = CompressionPolicyOverride(
            name="test",
            match_clients=["a"],
            compress_static_prefix=True,
        )
        assert override.compress_static_prefix is True
        assert override.mode is None

    def test_round_trip_config_with_policies(self) -> None:
        override = CompressionPolicyOverride(
            name="policy-1",
            match_clients=["client-a"],
            enabled=True,
            mode="safe",
        )
        config = CompressionConfig(policies=[override])
        dumped = config.model_dump()
        restored = CompressionConfig.model_validate(dumped)
        assert len(restored.policies) == 1
        assert restored.policies[0].name == "policy-1"
        assert restored.policies[0].enabled is True

    def test_multiple_valid_overrides(self) -> None:
        o1 = CompressionPolicyOverride(name="first", match_clients=["a"])
        o2 = CompressionPolicyOverride(name="second", match_models=["b"])
        config = CompressionConfig(policies=[o1, o2])
        assert len(config.policies) == 2

    def test_override_with_no_transforms_uses_defaults(self) -> None:
        override = CompressionPolicyOverride(name="test", match_clients=["a"])
        assert override.transforms is None
