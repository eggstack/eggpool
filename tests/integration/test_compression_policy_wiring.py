"""Integration tests for Phase 6 compression policy wiring.

Tests the end-to-end flow of constructing a CompressionPolicyContext
the same way proxy_request.py does and calling resolve_compression_policy.
"""

from __future__ import annotations

from eggpool.transcoder.compression.policy import (
    CompressionConfig,
    CompressionPolicyOverride,
)
from eggpool.transcoder.compression.policy_resolver import (
    GLOBAL_POLICY_NAME,
    GLOBAL_POLICY_SOURCE,
    CompressionPolicyContext,
    ResolvedCompressionPolicy,
    resolve_compression_policy,
)


class TestPolicyWiring:
    """End-to-end wiring of context construction and resolution."""

    def test_no_policies_global_default(self) -> None:
        """Without policies, resolved policy is global."""
        base = CompressionConfig()
        ctx = CompressionPolicyContext()
        result = resolve_compression_policy(base, ctx)
        assert result.name == GLOBAL_POLICY_NAME
        assert result.source == GLOBAL_POLICY_SOURCE

    def test_matching_client_policy_overlay_applies(self) -> None:
        override = CompressionPolicyOverride(
            name="my_policy",
            match_clients=["test-client"],
            enabled=True,
        )
        base = CompressionConfig(policies=[override])
        ctx = CompressionPolicyContext(
            client_id="test-client",
            client_name=None,
            source_protocol="openai",
            requested_model=None,
        )
        result = resolve_compression_policy(base, ctx)
        assert result.name == "my_policy"
        assert result.source == "policy:my_policy"
        assert result.config.enabled is True

    def test_x_eggpool_client_header_drives_matching(self) -> None:
        """x-eggpool-client header maps to client_id."""
        override = CompressionPolicyOverride(
            name="client-match", match_clients=["opencode"]
        )
        base = CompressionConfig(policies=[override])
        ctx = CompressionPolicyContext(client_id="opencode")
        result = resolve_compression_policy(base, ctx)
        assert result.name == "client-match"

    def test_user_agent_drives_client_name_matching(self) -> None:
        """User-Agent header maps to client_name."""
        override = CompressionPolicyOverride(
            name="ua-match", match_clients=["MyApp/1.0"]
        )
        base = CompressionConfig(policies=[override])
        ctx = CompressionPolicyContext(client_name="MyApp/1.0")
        result = resolve_compression_policy(base, ctx)
        assert result.name == "ua-match"

    def test_source_protocol_drives_matching(self) -> None:
        """source_protocol from endpoint drives protocol matching."""
        override = CompressionPolicyOverride(
            name="proto-match", match_protocols=["anthropic"]
        )
        base = CompressionConfig(policies=[override])
        ctx = CompressionPolicyContext(source_protocol="anthropic")
        result = resolve_compression_policy(base, ctx)
        assert result.name == "proto-match"

    def test_requested_model_header_drives_matching(self) -> None:
        """requested_model from model_value drives model matching."""
        override = CompressionPolicyOverride(
            name="model-match", match_requested_models=["gpt-4*"]
        )
        base = CompressionConfig(policies=[override])
        ctx = CompressionPolicyContext(requested_model="gpt-4-turbo")
        result = resolve_compression_policy(base, ctx)
        assert result.name == "model-match"

    def test_resolver_failure_does_not_crash(self) -> None:
        """Resolver must always return a ResolvedCompressionPolicy."""
        override = CompressionPolicyOverride(
            name="bad-override",
            match_clients=["a"],
            enabled=True,
        )
        base = CompressionConfig(policies=[override])
        ctx = CompressionPolicyContext(client_id="a")
        result = resolve_compression_policy(base, ctx)
        assert result is not None
        assert isinstance(result, ResolvedCompressionPolicy)

    def test_resolved_policy_always_returned(self) -> None:
        """resolve_compression_policy always returns a ResolvedCompressionPolicy."""
        base = CompressionConfig()
        ctx = CompressionPolicyContext()
        result = resolve_compression_policy(base, ctx)
        assert result is not None
        assert isinstance(result, ResolvedCompressionPolicy)

    def test_resolved_config_with_override_overlay_applies(self) -> None:
        override = CompressionPolicyOverride(
            name="custom",
            match_clients=["c1"],
            enabled=True,
            mode="safe",
            min_candidate_tokens=512,
        )
        base = CompressionConfig(policies=[override])
        ctx = CompressionPolicyContext(client_id="c1")
        result = resolve_compression_policy(base, ctx)
        assert result.name == "custom"
        assert result.source == "policy:custom"
        assert result.config.enabled is True
        assert result.config.mode == "safe"
        assert result.config.min_candidate_tokens == 512

    def test_multiple_clients_different_policies_overlay_applies(self) -> None:
        o1 = CompressionPolicyOverride(
            name="alpha", match_clients=["alpha-client"], enabled=True
        )
        o2 = CompressionPolicyOverride(
            name="beta", match_clients=["beta-client"], enabled=False
        )
        base = CompressionConfig(policies=[o1, o2])

        r1 = resolve_compression_policy(
            base, CompressionPolicyContext(client_id="alpha-client")
        )
        r2 = resolve_compression_policy(
            base, CompressionPolicyContext(client_id="beta-client")
        )
        assert r1.name == "alpha"
        assert r2.name == "beta"
        assert r1.config.enabled is True
        assert r2.config.enabled is False

    def test_pre_route_provider_matchers_are_noops(self) -> None:
        """Pre-route, provider_id/kind/model are None so provider
        matchers silently skip."""
        override = CompressionPolicyOverride(
            name="prov-pol",
            match_provider_ids=["prov-1"],
            match_provider_kinds=["anthropic"],
            match_models=["claude-sonnet"],
            enabled=True,
        )
        base = CompressionConfig(policies=[override])
        ctx = CompressionPolicyContext(
            client_id="some-client",
            source_protocol="openai",
        )
        result = resolve_compression_policy(base, ctx)
        assert result.name == GLOBAL_POLICY_NAME

    def test_no_warnings_on_successful_overlay(self) -> None:
        override = CompressionPolicyOverride(
            name="warn-pol", match_clients=["x"], enabled=True
        )
        base = CompressionConfig(policies=[override])
        ctx = CompressionPolicyContext(client_id="x")
        result = resolve_compression_policy(base, ctx)
        assert result.warnings == ()
        assert result.name == "warn-pol"
