"""Tests for resolve_compression_policy and helpers (Phase 6)."""

from __future__ import annotations

import pytest

from eggpool.transcoder.compression.policy import (
    CompressionConfig,
    CompressionPolicyOverride,
)
from eggpool.transcoder.compression.policy_resolver import (
    GLOBAL_POLICY_NAME,
    GLOBAL_POLICY_SOURCE,
    CompressionPolicyContext,
    ResolvedCompressionPolicy,
    _glob_match,
    _overlay_config,
    resolve_compression_policy,
)


class TestGlobMatch:
    """Simple * glob matching."""

    def test_exact_match(self) -> None:
        assert _glob_match("foo", "foo") is True

    def test_exact_no_match(self) -> None:
        assert _glob_match("foo", "bar") is False

    def test_none_value_always_false(self) -> None:
        assert _glob_match(None, "foo") is False
        assert _glob_match(None, "*foo") is False

    def test_prefix_glob(self) -> None:
        assert _glob_match("foobar", "foo*") is True
        assert _glob_match("barfoo", "foo*") is False

    def test_suffix_glob(self) -> None:
        assert _glob_match("barfoo", "*foo") is True
        assert _glob_match("foobar", "*foo") is False

    def test_contains_glob(self) -> None:
        assert _glob_match("myfoobar", "*foo*") is True
        assert _glob_match("mybar", "*foo*") is False

    def test_middle_star_not_supported(self) -> None:
        assert _glob_match("fooXXbar", "foo*bar") is False


class TestResolvedCompressionPolicyFrozen:
    """ResolvedCompressionPolicy is a frozen dataclass."""

    def test_cannot_mutate_name(self) -> None:
        config = CompressionConfig()
        resolved = ResolvedCompressionPolicy(name="test", source="test", config=config)
        with pytest.raises(AttributeError):
            resolved.name = "changed"  # type: ignore[misc]

    def test_cannot_mutate_config(self) -> None:
        config = CompressionConfig()
        resolved = ResolvedCompressionPolicy(name="test", source="test", config=config)
        with pytest.raises(AttributeError):
            resolved.config = CompressionConfig(enabled=True)  # type: ignore[misc]

    def test_as_dict_keys(self) -> None:
        config = CompressionConfig()
        resolved = ResolvedCompressionPolicy(
            name="test", source="policy:test", config=config
        )
        d = resolved.as_dict()
        assert d["name"] == "test"
        assert d["source"] == "policy:test"
        assert d["config_enabled"] is False
        assert d["config_mode"] == "observe"


class TestResolveCompressionPolicy:
    """resolve_compression_policy matching and merge logic."""

    def test_no_policies_returns_global(self) -> None:
        base = CompressionConfig()
        ctx = CompressionPolicyContext()
        result = resolve_compression_policy(base, ctx)
        assert result.name == GLOBAL_POLICY_NAME
        assert result.source == GLOBAL_POLICY_SOURCE
        assert result.matched_policy_names == ()
        assert result.warnings == ()

    def test_explicit_empty_overrides_returns_global(self) -> None:
        base = CompressionConfig()
        ctx = CompressionPolicyContext()
        result = resolve_compression_policy(base, ctx, overrides=[])
        assert result.name == GLOBAL_POLICY_NAME

    def test_exact_client_id_match_overlay_applies(self) -> None:
        override = CompressionPolicyOverride(
            name="client-pol", match_clients=["client-a"], enabled=True
        )
        base = CompressionConfig(policies=[override])
        ctx = CompressionPolicyContext(client_id="client-a")
        result = resolve_compression_policy(base, ctx)
        assert result.name == "client-pol"
        assert result.source == "policy:client-pol"
        assert result.config.enabled is True
        assert result.warnings == ()

    def test_exact_client_name_match_overlay_applies(self) -> None:
        override = CompressionPolicyOverride(
            name="name-pol", match_clients=["MyApp"], enabled=True
        )
        base = CompressionConfig(policies=[override])
        ctx = CompressionPolicyContext(client_name="MyApp")
        result = resolve_compression_policy(base, ctx)
        assert result.name == "name-pol"
        assert result.config.enabled is True

    def test_glob_client_match_overlay_applies(self) -> None:
        override = CompressionPolicyOverride(
            name="glob-pol", match_clients=["*claude*"], enabled=True
        )
        base = CompressionConfig(policies=[override])
        ctx = CompressionPolicyContext(client_id="my-claude-client")
        result = resolve_compression_policy(base, ctx)
        assert result.name == "glob-pol"
        assert result.config.enabled is True

    def test_match_by_requested_model_overlay_applies(self) -> None:
        override = CompressionPolicyOverride(
            name="model-pol",
            match_requested_models=["gpt-4*"],
            enabled=True,
        )
        base = CompressionConfig(policies=[override])
        ctx = CompressionPolicyContext(requested_model="gpt-4-turbo")
        result = resolve_compression_policy(base, ctx)
        assert result.name == "model-pol"
        assert result.config.enabled is True

    def test_match_by_source_protocol_overlay_applies(self) -> None:
        override = CompressionPolicyOverride(
            name="proto-pol", match_protocols=["openai"], enabled=True
        )
        base = CompressionConfig(policies=[override])
        ctx = CompressionPolicyContext(source_protocol="openai")
        result = resolve_compression_policy(base, ctx)
        assert result.name == "proto-pol"
        assert result.config.enabled is True

    def test_match_by_transcoded_true_overlay_applies(self) -> None:
        override = CompressionPolicyOverride(
            name="tc-true", match_transcoded=True, enabled=True
        )
        base = CompressionConfig(policies=[override])
        ctx = CompressionPolicyContext(transcoded=True)
        result = resolve_compression_policy(base, ctx)
        assert result.name == "tc-true"
        assert result.config.enabled is True

    def test_transcoded_no_match_when_different(self) -> None:
        override = CompressionPolicyOverride(name="tc-pol", match_transcoded=True)
        base = CompressionConfig(policies=[override])
        ctx = CompressionPolicyContext(transcoded=False)
        result = resolve_compression_policy(base, ctx)
        assert result.name == GLOBAL_POLICY_NAME

    def test_union_or_semantics_overlay_applies(self) -> None:
        """Override fires via union OR when any match field fires."""
        override = CompressionPolicyOverride(
            name="union-pol",
            match_clients=["client-a"],
            match_models=["model-x"],
            enabled=True,
        )
        base = CompressionConfig(policies=[override])
        ctx1 = CompressionPolicyContext(client_id="client-a")
        r1 = resolve_compression_policy(base, ctx1)
        assert r1.name == "union-pol"
        assert r1.config.enabled is True

        ctx2 = CompressionPolicyContext(resolved_model="model-x")
        r2 = resolve_compression_policy(base, ctx2)
        assert r2.name == "union-pol"
        assert r2.config.enabled is True

        ctx3 = CompressionPolicyContext(client_id="other", resolved_model="other")
        assert resolve_compression_policy(base, ctx3).name == GLOBAL_POLICY_NAME

    def test_last_match_wins_for_name(self) -> None:
        o1 = CompressionPolicyOverride(name="first", match_clients=["a"], enabled=True)
        o2 = CompressionPolicyOverride(
            name="second", match_clients=["a"], enabled=False
        )
        base = CompressionConfig(policies=[o1, o2])
        ctx = CompressionPolicyContext(client_id="a")
        result = resolve_compression_policy(base, ctx)
        assert result.name == "second"
        assert result.matched_policy_names == ("first", "second")
        assert result.config.enabled is False

    def test_provider_specific_matchers_noop_pre_route(self) -> None:
        override = CompressionPolicyOverride(
            name="provider-pol",
            match_provider_ids=["prov-1"],
            match_provider_kinds=["anthropic"],
            match_models=["claude-sonnet"],
            enabled=True,
        )
        base = CompressionConfig(policies=[override])
        ctx = CompressionPolicyContext()
        result = resolve_compression_policy(base, ctx)
        assert result.name == GLOBAL_POLICY_NAME

    def test_provider_id_match_overlay_applies(self) -> None:
        override = CompressionPolicyOverride(
            name="prov-pol", match_provider_ids=["prov-1"], enabled=True
        )
        base = CompressionConfig(policies=[override])
        ctx = CompressionPolicyContext(provider_id="prov-1")
        result = resolve_compression_policy(base, ctx)
        assert result.name == "prov-pol"
        assert result.config.enabled is True

    def test_overlay_config_returns_new_config(self) -> None:
        base = CompressionConfig(enabled=False)
        override = CompressionPolicyOverride(name="default", enabled=True)
        result = _overlay_config(base, override)
        assert result is not base
        assert base.enabled is False
        assert result.enabled is True

    def test_overlay_config_does_not_mutate_base(self) -> None:
        base = CompressionConfig(enabled=False, min_candidate_tokens=2048)
        override = CompressionPolicyOverride(name="default", min_candidate_tokens=1024)
        _overlay_config(base, override)
        assert base.min_candidate_tokens == 2048

    def test_overlay_config_filters_match_fields(self) -> None:
        """Match fields are not propagated to the merged CompressionConfig."""
        base = CompressionConfig(enabled=False)
        override = CompressionPolicyOverride(
            name="test", match_clients=["a"], enabled=True
        )
        result = _overlay_config(base, override)
        assert result.enabled is True
        assert not hasattr(result, "match_clients")

    def test_resolver_no_warnings_on_success(self) -> None:
        override = CompressionPolicyOverride(
            name="ok-pol", match_clients=["a"], enabled=True
        )
        base = CompressionConfig(policies=[override])
        ctx = CompressionPolicyContext(client_id="a")
        result = resolve_compression_policy(base, ctx)
        assert result.warnings == ()

    def test_resolver_multiple_matching_overlay_all_apply(self) -> None:
        o1 = CompressionPolicyOverride(name="first", match_clients=["a"], enabled=True)
        o2 = CompressionPolicyOverride(
            name="second", match_clients=["a"], enabled=False
        )
        base = CompressionConfig(policies=[o1, o2])
        ctx = CompressionPolicyContext(client_id="a")
        result = resolve_compression_policy(base, ctx)
        assert result.matched_policy_names == ("first", "second")
        assert result.warnings == ()
        assert result.config.enabled is False

    def test_non_matching_override_ignored(self) -> None:
        override = CompressionPolicyOverride(
            name="other-client", match_clients=["b"], enabled=True
        )
        base = CompressionConfig(policies=[override])
        ctx = CompressionPolicyContext(client_id="a")
        result = resolve_compression_policy(base, ctx)
        assert result.name == GLOBAL_POLICY_NAME
        assert result.config.enabled is False

    def test_catch_all_matches(self) -> None:
        """Catch-all override (no match fields, name='default') always fires."""
        override = CompressionPolicyOverride(name="default", enabled=True)
        base = CompressionConfig(policies=[override])
        ctx = CompressionPolicyContext()
        result = resolve_compression_policy(base, ctx)
        assert result.name == "default"
        assert result.source == "policy:default"
        assert result.config.enabled is True

    def test_glob_foo_star_matches_prefix_only(self) -> None:
        override = CompressionPolicyOverride(name="pre-pol", match_clients=["foo*"])
        base = CompressionConfig(policies=[override])
        assert (
            resolve_compression_policy(
                base, CompressionPolicyContext(client_id="foobar")
            ).name
            == "pre-pol"
        )
        assert (
            resolve_compression_policy(
                base, CompressionPolicyContext(client_id="barfoo")
            ).name
            == GLOBAL_POLICY_NAME
        )

    def test_glob_star_foo_matches_suffix_only(self) -> None:
        override = CompressionPolicyOverride(name="suf-pol", match_clients=["*foo"])
        base = CompressionConfig(policies=[override])
        assert (
            resolve_compression_policy(
                base, CompressionPolicyContext(client_id="barfoo")
            ).name
            == "suf-pol"
        )
        assert (
            resolve_compression_policy(
                base, CompressionPolicyContext(client_id="foobar")
            ).name
            == GLOBAL_POLICY_NAME
        )

    def test_glob_star_foo_star_matches_substring(self) -> None:
        override = CompressionPolicyOverride(name="sub-pol", match_clients=["*code*"])
        base = CompressionConfig(policies=[override])
        assert (
            resolve_compression_policy(
                base, CompressionPolicyContext(client_id="opencode-go")
            ).name
            == "sub-pol"
        )

    def test_match_by_source_protocol_no_match(self) -> None:
        override = CompressionPolicyOverride(
            name="proto-pol", match_protocols=["anthropic"]
        )
        base = CompressionConfig(policies=[override])
        ctx = CompressionPolicyContext(source_protocol="openai")
        result = resolve_compression_policy(base, ctx)
        assert result.name == GLOBAL_POLICY_NAME

    def test_match_by_transcoded_false(self) -> None:
        override = CompressionPolicyOverride(name="tc-false", match_transcoded=False)
        base = CompressionConfig(policies=[override])
        ctx = CompressionPolicyContext(transcoded=False)
        result = resolve_compression_policy(base, ctx)
        assert result.name == "tc-false"

    def test_client_name_matches_via_match_clients(self) -> None:
        """match_clients checks both client_id and client_name."""
        override = CompressionPolicyOverride(name="ua-pol", match_clients=["MyApp/1.0"])
        base = CompressionConfig(policies=[override])
        ctx = CompressionPolicyContext(client_name="MyApp/1.0")
        result = resolve_compression_policy(base, ctx)
        assert result.name == "ua-pol"

    def test_resolved_config_with_override_overlay_applies(self) -> None:
        override = CompressionPolicyOverride(
            name="my-pol",
            match_clients=["c1"],
            enabled=True,
            mode="safe",
            min_candidate_tokens=512,
        )
        base = CompressionConfig(policies=[override])
        ctx = CompressionPolicyContext(client_id="c1")
        result = resolve_compression_policy(base, ctx)
        assert result.name == "my-pol"
        assert result.source == "policy:my-pol"
        assert result.config.enabled is True
        assert result.config.mode == "safe"
        assert result.config.min_candidate_tokens == 512


class TestTransformsMerge:
    """Transform fields merge per-key, not wholesale replace."""

    def test_base_enabled_override_adds_field(self) -> None:
        base = CompressionConfig(
            enabled=True,
            transforms={"fold_repeated_lines": True},
        )
        override = CompressionPolicyOverride(
            name="merge-pol",
            transforms={"compact_logs": True},
        )
        result = _overlay_config(base, override)
        assert result.enabled is True
        assert result.transforms.fold_repeated_lines is True
        assert result.transforms.compact_logs is True

    def test_override_disables_transform(self) -> None:
        base = CompressionConfig(
            enabled=True,
            transforms={"fold_repeated_lines": True},
        )
        override = CompressionPolicyOverride(
            name="off-pol",
            transforms={"fold_repeated_lines": False},
        )
        result = _overlay_config(base, override)
        assert result.transforms.fold_repeated_lines is False

    def test_override_none_transform_keeps_base(self) -> None:
        base = CompressionConfig(
            enabled=True,
            transforms={"fold_repeated_lines": True},
        )
        override = CompressionPolicyOverride(
            name="none-pol",
            transforms={"compact_logs": True},
        )
        result = _overlay_config(base, override)
        assert result.transforms.fold_repeated_lines is True
        assert result.transforms.compact_logs is True
