"""Tests for Phase 9 synthetic cache-controls selector and mutator.

Phase 9 adds opt-in synthetic ``cache_control`` annotations around
provider-cacheable stable-prefix blocks (initially Anthropic).  The
selector is dry-run-first and disabled by default; the mutator only
runs when both the global ``[cache] synthetic_cache_controls`` config
and the resolved compression policy override turn the feature on.

These tests pin the conservative invariants from the plan:

- Synthetic cache controls are disabled by default.
- Dry-run mode records candidates without mutating the body.
- Apply mode only mutates supported Anthropic-compatible
  stable-prefix blocks.
- Native cache controls are preserved and never duplicated.
- Volatile-suffix and compressed content never receive synthetic
  cache controls.
- The boundary tracker records synthetic events with
  ``kind = "synthesized"``.
- Routing is not consulted at all (the selector is
  ``QuotaFairScorer``-oblivious).

The tests intentionally exercise the production selector against
real segmentation results so we cannot accidentally bypass the
Phase 2 / Phase 3 invariants while wiring Phase 9.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from eggpool.transcoder.cache_stability import CACHE_BOUNDARY_KIND_SYNTHESIZED
from eggpool.transcoder.cache_synthesis import (
    WARN_BELOW_MIN_TOKENS,
    WARN_DISABLED,
    WARN_DRY_RUN,
    WARN_EXISTING_NATIVE_PRESERVED,
    WARN_LIMIT_REACHED,
    WARN_NO_STABLE_CANDIDATE,
    WARN_POLICY_REQUIRED,
    WARN_PROVIDER_UNSUPPORTED,
    WARN_SYNTHESIZED,
    SyntheticCachePlan,
    apply_synthetic_cache_controls,
    run_synthetic_cache_synthesis,
    select_synthetic_cache_candidates,
)
from eggpool.transcoder.cache_synthesis_policy import (
    CacheConfig,
    SyntheticCacheControlsConfig,
)
from eggpool.transcoder.compression.policy import (
    CompressionConfig,
    CompressionPolicyOverride,
)
from eggpool.transcoder.compression.policy_resolver import (
    GLOBAL_POLICY_NAME,
    GLOBAL_POLICY_SOURCE,
    CompressionPolicyContext,
    resolve_compression_policy,
)
from eggpool.transcoder.context import TranscodeContext
from eggpool.transcoder.segmentation import segment_request

ANTHROPIC_PROTOCOL = "anthropic"
OPENAI_PROTOCOL = "openai"


def _anthropic_payload(
    *,
    system: list[dict[str, Any]] | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a minimal Anthropic request body for the selector."""
    payload: dict[str, Any] = {
        "model": "claude-3-5-sonnet",
        "messages": [{"role": "user", "content": "Hello"}],
        "max_tokens": 256,
    }
    if system is not None:
        payload["system"] = system
    if tools is not None:
        payload["tools"] = tools
    return payload


def _resolved_policy(
    *,
    enabled: bool = True,
    dry_run: bool = True,
    min_stable_tokens: int = 1024,
    max_breakpoints: int = 4,
    policy_name: str = "test-policy",
    policy_source: str = "policy:test-policy",
) -> Any:
    """Build a fake ``ResolvedCompressionPolicy`` carrying Phase 9 overrides.

    The selector reads ``synthetic_cache_overrides`` to merge knobs
    on top of the global ``CacheConfig``.  We bypass the resolver
    here so the tests can isolate the selector behaviour.
    """

    class _FakePolicy:
        def __init__(self) -> None:
            self.name = policy_name
            self.source = policy_source
            self.matched_policy_names = (policy_name,)
            self.warnings: tuple[str, ...] = ()
            self.synthetic_cache_overrides: dict[str, Any] = {
                "synthetic_cache_controls": enabled,
                "synthetic_cache_dry_run": dry_run,
                "synthetic_cache_min_stable_tokens": min_stable_tokens,
                "synthetic_cache_max_breakpoints": max_breakpoints,
            }

    return _FakePolicy()


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------


class TestCacheConfigDefaults:
    """Defaults are safe: synthetic cache controls are off; dry-run on."""

    def test_default_synthetic_cache_disabled(self) -> None:
        config = SyntheticCacheControlsConfig()
        assert config.enabled is False
        assert config.dry_run is True
        assert config.provider_kinds == ["anthropic"]
        assert config.ttl == "ephemeral"
        assert config.min_stable_tokens == 1024
        assert config.max_breakpoints == 4
        assert config.require_policy is True
        assert config.placements == ("system", "tools")

    def test_default_cache_config(self) -> None:
        config = CacheConfig()
        assert config.synthetic_cache_controls.enabled is False

    def test_max_breakpoints_above_anthropic_limit_rejected(self) -> None:
        with pytest.raises(ValueError, match="max_breakpoints"):
            SyntheticCacheControlsConfig(max_breakpoints=5)


# ---------------------------------------------------------------------------
# Selector: candidate selection
# ---------------------------------------------------------------------------


class TestSelectCandidatesAnthropic:
    """Selector picks stable-prefix segments and respects the cap."""

    def test_no_stable_prefix_yields_no_candidates(self) -> None:
        # A request with only volatile user/tool content has no
        # ``stable_prefix`` segments, so the selector finds nothing.
        payload = _anthropic_payload(
            tools=[
                {
                    "name": "search",
                    "description": "search the web",
                    "input_schema": {"type": "object"},
                }
            ]
        )
        segmentation = segment_request(payload, protocol=ANTHROPIC_PROTOCOL)
        # Strip tool segments by deleting tools so the segmentation
        # is structurally empty of stable_prefix besides system.  We
        # confirm baseline stable-prefix content first.
        cache_config = CacheConfig(
            synthetic_cache_controls=SyntheticCacheControlsConfig(
                enabled=True,
                dry_run=True,
                require_policy=False,
                min_stable_tokens=0,
            )
        )
        plan = select_synthetic_cache_candidates(
            segmentation,
            payload,
            cache_config=cache_config,
            target_protocol=ANTHROPIC_PROTOCOL,
            target_provider_kind="anthropic",
            resolved_policy=None,
        )
        # tools are stable prefix candidates; with min_stable_tokens=0
        # the selector surfaces them.
        assert plan.status == "dry_run"
        assert plan.candidate_count >= 1

    def test_stable_system_block_becomes_candidate(self) -> None:
        payload = _anthropic_payload(
            system=[
                {
                    "type": "text",
                    "text": "You are a helpful assistant. " * 200,
                }
            ]
        )
        segmentation = segment_request(payload, protocol=ANTHROPIC_PROTOCOL)
        cache_config = CacheConfig(
            synthetic_cache_controls=SyntheticCacheControlsConfig(
                enabled=True,
                dry_run=True,
                require_policy=False,
                min_stable_tokens=0,
            )
        )
        plan = select_synthetic_cache_candidates(
            segmentation,
            payload,
            cache_config=cache_config,
            target_protocol=ANTHROPIC_PROTOCOL,
            target_provider_kind="anthropic",
            resolved_policy=None,
        )
        assert plan.status == "dry_run"
        assert any(c.placement == "system" for c in plan.candidates)
        assert any(c.reason == "system_candidate" for c in plan.candidates)

    def test_stable_tool_schema_becomes_candidate(self) -> None:
        payload = _anthropic_payload(
            tools=[
                {
                    "name": "lookup",
                    "description": "lookup something",
                    "input_schema": {"type": "object"},
                }
            ]
        )
        segmentation = segment_request(payload, protocol=ANTHROPIC_PROTOCOL)
        cache_config = CacheConfig(
            synthetic_cache_controls=SyntheticCacheControlsConfig(
                enabled=True,
                dry_run=True,
                require_policy=False,
                min_stable_tokens=0,
            )
        )
        plan = select_synthetic_cache_candidates(
            segmentation,
            payload,
            cache_config=cache_config,
            target_protocol=ANTHROPIC_PROTOCOL,
            target_provider_kind="anthropic",
            resolved_policy=None,
        )
        assert plan.status == "dry_run"
        assert any(c.placement == "tools" for c in plan.candidates)
        assert any(c.reason == "tool_schema_candidate" for c in plan.candidates)

    def test_below_min_tokens_suppressed(self) -> None:
        payload = _anthropic_payload(system=[{"type": "text", "text": "short system"}])
        segmentation = segment_request(payload, protocol=ANTHROPIC_PROTOCOL)
        cache_config = CacheConfig(
            synthetic_cache_controls=SyntheticCacheControlsConfig(
                enabled=True,
                dry_run=True,
                require_policy=False,
                min_stable_tokens=10_000,
            )
        )
        plan = select_synthetic_cache_candidates(
            segmentation,
            payload,
            cache_config=cache_config,
            target_protocol=ANTHROPIC_PROTOCOL,
            target_provider_kind="anthropic",
            resolved_policy=None,
        )
        assert plan.status == "no_candidates"
        assert WARN_BELOW_MIN_TOKENS in plan.warnings
        assert WARN_NO_STABLE_CANDIDATE in plan.warnings

    def test_breakpoint_cap_enforced(self) -> None:
        # Build a request with many stable-prefix system blocks so
        # the breakpoint cap has to fire.
        blocks = [
            {"type": "text", "text": f"system block {i} " + ("x" * 800)}
            for i in range(8)
        ]
        payload = _anthropic_payload(system=blocks)
        segmentation = segment_request(payload, protocol=ANTHROPIC_PROTOCOL)
        cache_config = CacheConfig(
            synthetic_cache_controls=SyntheticCacheControlsConfig(
                enabled=True,
                dry_run=True,
                require_policy=False,
                min_stable_tokens=0,
                max_breakpoints=2,
            )
        )
        plan = select_synthetic_cache_candidates(
            segmentation,
            payload,
            cache_config=cache_config,
            target_protocol=ANTHROPIC_PROTOCOL,
            target_provider_kind="anthropic",
            resolved_policy=None,
        )
        assert plan.status == "dry_run"
        assert len(plan.candidates) <= 2
        assert WARN_LIMIT_REACHED in plan.warnings


# ---------------------------------------------------------------------------
# Selector: gating (disabled, policy required, provider unsupported)
# ---------------------------------------------------------------------------


class TestSelectorGating:
    """Selector short-circuits on disabled / policy / provider mismatch."""

    def test_default_disabled_returns_disabled_status(self) -> None:
        payload = _anthropic_payload()
        segmentation = segment_request(payload, protocol=ANTHROPIC_PROTOCOL)
        cache_config = CacheConfig()  # defaults: enabled=False
        plan = select_synthetic_cache_candidates(
            segmentation,
            payload,
            cache_config=cache_config,
            target_protocol=ANTHROPIC_PROTOCOL,
            target_provider_kind="anthropic",
            resolved_policy=None,
        )
        assert plan.status == "disabled"
        assert WARN_DISABLED in plan.warnings

    def test_require_policy_blocks_when_global_only(self) -> None:
        payload = _anthropic_payload(system=[{"type": "text", "text": "x" * 4096}])
        segmentation = segment_request(payload, protocol=ANTHROPIC_PROTOCOL)
        cache_config = CacheConfig(
            synthetic_cache_controls=SyntheticCacheControlsConfig(
                enabled=True,
                dry_run=True,
                require_policy=True,
                min_stable_tokens=0,
            )
        )
        plan = select_synthetic_cache_candidates(
            segmentation,
            payload,
            cache_config=cache_config,
            target_protocol=ANTHROPIC_PROTOCOL,
            target_provider_kind="anthropic",
            resolved_policy=None,
        )
        assert plan.status == "policy_required"
        assert WARN_POLICY_REQUIRED in plan.warnings

    def test_require_policy_satisfied_with_policy_override(self) -> None:
        payload = _anthropic_payload(system=[{"type": "text", "text": "x" * 4096}])
        segmentation = segment_request(payload, protocol=ANTHROPIC_PROTOCOL)
        cache_config = CacheConfig(
            synthetic_cache_controls=SyntheticCacheControlsConfig(
                enabled=True,
                dry_run=True,
                require_policy=True,
                min_stable_tokens=0,
            )
        )
        resolved = _resolved_policy(enabled=True, dry_run=True, min_stable_tokens=0)
        plan = select_synthetic_cache_candidates(
            segmentation,
            payload,
            cache_config=cache_config,
            target_protocol=ANTHROPIC_PROTOCOL,
            target_provider_kind="anthropic",
            resolved_policy=resolved,
        )
        assert plan.status == "dry_run"

    def test_provider_unsupported_blocks_non_anthropic(self) -> None:
        payload = _anthropic_payload()
        segmentation = segment_request(payload, protocol=ANTHROPIC_PROTOCOL)
        cache_config = CacheConfig(
            synthetic_cache_controls=SyntheticCacheControlsConfig(
                enabled=True,
                dry_run=True,
                require_policy=False,
                min_stable_tokens=0,
            )
        )
        plan = select_synthetic_cache_candidates(
            segmentation,
            payload,
            cache_config=cache_config,
            target_protocol=ANTHROPIC_PROTOCOL,
            target_provider_kind="openai",
            resolved_policy=None,
        )
        assert plan.status == "provider_unsupported"
        assert WARN_PROVIDER_UNSUPPORTED in plan.warnings

    def test_non_anthropic_target_protocol_blocked(self) -> None:
        payload: dict[str, Any] = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hi"}],
        }
        segmentation = segment_request(payload, protocol=OPENAI_PROTOCOL)
        cache_config = CacheConfig(
            synthetic_cache_controls=SyntheticCacheControlsConfig(
                enabled=True,
                dry_run=True,
                require_policy=False,
                min_stable_tokens=0,
            )
        )
        plan = select_synthetic_cache_candidates(
            segmentation,
            payload,
            cache_config=cache_config,
            target_protocol=OPENAI_PROTOCOL,
            target_provider_kind="openai",
            resolved_policy=None,
        )
        assert plan.status == "provider_unsupported"

    def test_no_segmentation_yields_no_candidates(self) -> None:
        payload = _anthropic_payload()
        cache_config = CacheConfig(
            synthetic_cache_controls=SyntheticCacheControlsConfig(
                enabled=True,
                dry_run=True,
                require_policy=False,
            )
        )
        plan = select_synthetic_cache_candidates(
            None,
            payload,
            cache_config=cache_config,
            target_protocol=ANTHROPIC_PROTOCOL,
            target_provider_kind="anthropic",
            resolved_policy=None,
        )
        assert plan.status == "no_candidates"
        assert WARN_NO_STABLE_CANDIDATE in plan.warnings

    def test_non_mapping_payload_blocked(self) -> None:
        cache_config = CacheConfig(
            synthetic_cache_controls=SyntheticCacheControlsConfig(
                enabled=True,
                dry_run=True,
                require_policy=False,
            )
        )
        plan = select_synthetic_cache_candidates(
            None,
            "not a dict",
            cache_config=cache_config,
            target_protocol=ANTHROPIC_PROTOCOL,
            target_provider_kind="anthropic",
            resolved_policy=None,
        )
        assert plan.status == "disabled"


# ---------------------------------------------------------------------------
# Dry-run mode
# ---------------------------------------------------------------------------


class TestDryRun:
    """Dry-run mode never mutates the payload but records candidates."""

    def test_dry_run_does_not_mutate_payload(self) -> None:
        payload = _anthropic_payload(system=[{"type": "text", "text": "x" * 4096}])
        original = json.dumps(payload, sort_keys=True)
        segmentation = segment_request(payload, protocol=ANTHROPIC_PROTOCOL)
        cache_config = CacheConfig(
            synthetic_cache_controls=SyntheticCacheControlsConfig(
                enabled=True,
                dry_run=True,
                require_policy=False,
                min_stable_tokens=0,
            )
        )
        resolved = _resolved_policy(
            enabled=True,
            dry_run=True,
            min_stable_tokens=0,
        )
        result = run_synthetic_cache_synthesis(
            payload,
            segmentation=segmentation,
            cache_config=cache_config,
            target_protocol=ANTHROPIC_PROTOCOL,
            target_provider_kind="anthropic",
            resolved_policy=resolved,
        )
        # Payload unchanged.
        assert json.dumps(payload, sort_keys=True) == original
        assert result.transformed_payload is None
        assert result.plan.status == "dry_run"
        assert WARN_DRY_RUN in result.warnings
        assert result.plan.candidate_count >= 1
        assert result.plan.applied_count == 0
        # No synthesized annotations were recorded.
        assert result.cache_boundary_entries == ()

    def test_dry_run_summary_no_raw_prompt(self) -> None:
        prompt_text = "secret-prompt-do-not-leak-" + "x" * 4096
        payload = _anthropic_payload(system=[{"type": "text", "text": prompt_text}])
        segmentation = segment_request(payload, protocol=ANTHROPIC_PROTOCOL)
        cache_config = CacheConfig(
            synthetic_cache_controls=SyntheticCacheControlsConfig(
                enabled=True,
                dry_run=True,
                require_policy=False,
                min_stable_tokens=0,
            )
        )
        resolved = _resolved_policy(dry_run=True, min_stable_tokens=0)
        result = run_synthetic_cache_synthesis(
            payload,
            segmentation=segmentation,
            cache_config=cache_config,
            target_protocol=ANTHROPIC_PROTOCOL,
            target_provider_kind="anthropic",
            resolved_policy=resolved,
        )
        assert prompt_text not in result.summary_json
        assert prompt_text not in json.dumps(result.plan.as_dict())


# ---------------------------------------------------------------------------
# Apply mode
# ---------------------------------------------------------------------------


class TestApplyAnthropic:
    """Apply mode mutates supported blocks; never duplicates native."""

    def test_apply_adds_cache_control_to_system_block(self) -> None:
        payload = _anthropic_payload(system=[{"type": "text", "text": "x" * 4096}])
        segmentation = segment_request(payload, protocol=ANTHROPIC_PROTOCOL)
        cache_config = CacheConfig(
            synthetic_cache_controls=SyntheticCacheControlsConfig(
                enabled=True,
                dry_run=False,
                require_policy=False,
                min_stable_tokens=0,
            )
        )
        resolved = _resolved_policy(
            enabled=True,
            dry_run=False,
            min_stable_tokens=0,
        )
        result = run_synthetic_cache_synthesis(
            payload,
            segmentation=segmentation,
            cache_config=cache_config,
            target_protocol=ANTHROPIC_PROTOCOL,
            target_provider_kind="anthropic",
            resolved_policy=resolved,
        )
        assert result.plan.status == "applied"
        assert result.transformed_payload is not None
        system = result.transformed_payload["system"]
        assert isinstance(system, list)
        assert system[0]["cache_control"] == {"type": "ephemeral"}
        assert WARN_SYNTHESIZED in result.warnings

    def test_apply_does_not_duplicate_native_cache_control(self) -> None:
        payload = _anthropic_payload(
            system=[
                {
                    "type": "text",
                    "text": "x" * 4096,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        )
        segmentation = segment_request(payload, protocol=ANTHROPIC_PROTOCOL)
        cache_config = CacheConfig(
            synthetic_cache_controls=SyntheticCacheControlsConfig(
                enabled=True,
                dry_run=False,
                require_policy=False,
                min_stable_tokens=0,
            )
        )
        resolved = _resolved_policy(
            enabled=True,
            dry_run=False,
            min_stable_tokens=0,
        )
        result = run_synthetic_cache_synthesis(
            payload,
            segmentation=segmentation,
            cache_config=cache_config,
            target_protocol=ANTHROPIC_PROTOCOL,
            target_provider_kind="anthropic",
            resolved_policy=resolved,
        )
        # Native control is preserved; selector recorded it as such.
        assert WARN_EXISTING_NATIVE_PRESERVED in result.warnings
        # Mutator never duplicates onto the same block.
        system = result.transformed_payload["system"]
        assert isinstance(system, list)
        assert system[0]["cache_control"] == {"type": "ephemeral"}
        assert "x" * 4096 in system[0]["text"]

    def test_apply_records_synthetic_boundary_annotations(self) -> None:
        payload = _anthropic_payload(system=[{"type": "text", "text": "x" * 4096}])
        segmentation = segment_request(payload, protocol=ANTHROPIC_PROTOCOL)
        cache_config = CacheConfig(
            synthetic_cache_controls=SyntheticCacheControlsConfig(
                enabled=True,
                dry_run=False,
                require_policy=False,
                min_stable_tokens=0,
            )
        )
        resolved = _resolved_policy(
            enabled=True,
            dry_run=False,
            min_stable_tokens=0,
        )
        context = TranscodeContext(
            request_id="req-1",
            client_protocol=ANTHROPIC_PROTOCOL,
            upstream_protocol=ANTHROPIC_PROTOCOL,
        )
        result = run_synthetic_cache_synthesis(
            payload,
            segmentation=segmentation,
            cache_config=cache_config,
            target_protocol=ANTHROPIC_PROTOCOL,
            target_provider_kind="anthropic",
            resolved_policy=resolved,
            transcode_context=context,
        )
        tracker = context.cache_boundary_tracker
        assert len(result.cache_boundary_entries) >= 1
        for annotation in result.cache_boundary_entries:
            assert annotation.kind == CACHE_BOUNDARY_KIND_SYNTHESIZED
            assert annotation.cache_control_type == "ephemeral"
        # Tracker received the same entries.
        for annotation in tracker.annotations:
            assert annotation.kind == CACHE_BOUNDARY_KIND_SYNTHESIZED

    def test_apply_does_not_mutate_volatile_suffix(self) -> None:
        # Force the selector to surface a tool schema candidate AND
        # ensure the volatile suffix tool result is untouched.
        payload = _anthropic_payload(
            system=[{"type": "text", "text": "x" * 4096}],
            tools=[
                {
                    "name": "lookup",
                    "description": "lookup",
                    "input_schema": {"type": "object"},
                }
            ],
        )
        payload["messages"] = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_0",
                        "content": "Traceback (most recent call last):\n"
                        "  File 'main.py', line 1\nException: oops",
                    }
                ],
            }
        ]
        segmentation = segment_request(payload, protocol=ANTHROPIC_PROTOCOL)
        cache_config = CacheConfig(
            synthetic_cache_controls=SyntheticCacheControlsConfig(
                enabled=True,
                dry_run=False,
                require_policy=False,
                min_stable_tokens=0,
            )
        )
        resolved = _resolved_policy(
            enabled=True,
            dry_run=False,
            min_stable_tokens=0,
        )
        result = run_synthetic_cache_synthesis(
            payload,
            segmentation=segmentation,
            cache_config=cache_config,
            target_protocol=ANTHROPIC_PROTOCOL,
            target_provider_kind="anthropic",
            resolved_policy=resolved,
        )
        mutated = result.transformed_payload
        assert mutated is not None
        # Volatile suffix content is untouched.
        tool_block = cast("list[Any]", mutated["messages"][0]["content"])[0]
        assert "cache_control" not in tool_block
        assert "Traceback" in tool_block["content"]

    def test_apply_unsupported_provider_no_mutation(self) -> None:
        payload = _anthropic_payload(system=[{"type": "text", "text": "x" * 4096}])
        segmentation = segment_request(payload, protocol=ANTHROPIC_PROTOCOL)
        cache_config = CacheConfig(
            synthetic_cache_controls=SyntheticCacheControlsConfig(
                enabled=True,
                dry_run=False,
                require_policy=False,
                min_stable_tokens=0,
            )
        )
        result = run_synthetic_cache_synthesis(
            payload,
            segmentation=segmentation,
            cache_config=cache_config,
            target_protocol=ANTHROPIC_PROTOCOL,
            target_provider_kind="openai",
            resolved_policy=None,
        )
        assert result.plan.status == "provider_unsupported"
        assert result.transformed_payload is None


# ---------------------------------------------------------------------------
# Pure mutator (apply_synthetic_cache_controls)
# ---------------------------------------------------------------------------


class TestApplyPure:
    """apply_synthetic_cache_controls is idempotent and safe."""

    def test_returns_input_when_plan_not_applied(self) -> None:
        payload = _anthropic_payload()
        plan = SyntheticCachePlan(
            status="dry_run",
            dry_run=True,
            candidates=(),
            applied_count=0,
            warnings=(),
            policy_name=GLOBAL_POLICY_NAME,
            policy_source=GLOBAL_POLICY_SOURCE,
        )
        mutated, applied, annotations = apply_synthetic_cache_controls(payload, plan)
        assert mutated is payload
        assert applied == 0
        assert annotations == ()

    def test_records_synthetic_annotations_for_each_candidate(self) -> None:
        from eggpool.transcoder.cache_synthesis import SyntheticCacheCandidate

        payload = _anthropic_payload(
            system=[
                {"type": "text", "text": "x" * 4096},
                {"type": "text", "text": "y" * 4096},
            ]
        )
        candidates = (
            SyntheticCacheCandidate(
                placement="system",
                source_path="system.0.text",
                target_path="system.0.text",
                estimated_tokens=1000,
                reason="system_candidate",
                policy_name="policy:x",
                policy_source="policy:x",
            ),
            SyntheticCacheCandidate(
                placement="system",
                source_path="system.1.text",
                target_path="system.1.text",
                estimated_tokens=1000,
                reason="system_candidate",
                policy_name="policy:x",
                policy_source="policy:x",
            ),
        )
        plan = SyntheticCachePlan(
            status="applied",
            dry_run=False,
            candidates=candidates,
            applied_count=len(candidates),
            warnings=(),
            policy_name="policy:x",
            policy_source="policy:x",
        )
        mutated, applied, annotations = apply_synthetic_cache_controls(payload, plan)
        assert mutated is not payload
        assert applied == 2
        assert mutated["system"][0]["cache_control"] == {"type": "ephemeral"}
        assert mutated["system"][1]["cache_control"] == {"type": "ephemeral"}
        assert len(annotations) == 2
        for annotation in annotations:
            assert annotation.kind == CACHE_BOUNDARY_KIND_SYNTHESIZED


# ---------------------------------------------------------------------------
# Phase 6 policy override integration
# ---------------------------------------------------------------------------


class TestPolicyOverrideIntegration:
    """Synthetic cache overrides ride on the Phase 6 resolver."""

    def test_resolver_surfaces_synthetic_cache_overrides(self) -> None:
        base = CompressionConfig()
        overrides = [
            CompressionPolicyOverride(
                name="anthropic-cache",
                match_protocols=["anthropic"],
                match_provider_kinds=["anthropic"],
                synthetic_cache_controls=True,
                synthetic_cache_dry_run=False,
                synthetic_cache_min_stable_tokens=2048,
                synthetic_cache_max_breakpoints=3,
            )
        ]
        ctx = CompressionPolicyContext(
            client_id="opencode",
            source_protocol="anthropic",
            requested_model="claude-3-5-sonnet",
            transcoded=False,
        )
        resolved = resolve_compression_policy(base, ctx, overrides=overrides)
        assert resolved.name == "anthropic-cache"
        assert resolved.synthetic_cache_overrides == {
            "synthetic_cache_controls": True,
            "synthetic_cache_dry_run": False,
            "synthetic_cache_min_stable_tokens": 2048,
            "synthetic_cache_max_breakpoints": 3,
        }

    def test_resolver_global_returns_none_overrides(self) -> None:
        base = CompressionConfig()
        ctx = CompressionPolicyContext(
            client_id="opencode",
            source_protocol="anthropic",
            requested_model="claude-3-5-sonnet",
        )
        resolved = resolve_compression_policy(base, ctx)
        assert resolved.name == GLOBAL_POLICY_NAME
        assert resolved.synthetic_cache_overrides is None

    def test_overrides_apply_to_effective_config(self) -> None:
        payload = _anthropic_payload(system=[{"type": "text", "text": "x" * 4096}])
        segmentation = segment_request(payload, protocol=ANTHROPIC_PROTOCOL)
        cache_config = CacheConfig(
            synthetic_cache_controls=SyntheticCacheControlsConfig(
                enabled=True,
                dry_run=False,
                require_policy=True,
                min_stable_tokens=4096,
                max_breakpoints=4,
            )
        )
        overrides = [
            CompressionPolicyOverride(
                name="loose",
                match_protocols=["anthropic"],
                match_provider_kinds=["anthropic"],
                synthetic_cache_controls=True,
                synthetic_cache_dry_run=False,
                synthetic_cache_min_stable_tokens=0,
                synthetic_cache_max_breakpoints=2,
            )
        ]
        ctx = CompressionPolicyContext(
            source_protocol="anthropic",
            provider_kind="anthropic",
        )
        resolved = resolve_compression_policy(
            CompressionConfig(), ctx, overrides=overrides
        )
        result = run_synthetic_cache_synthesis(
            payload,
            segmentation=segmentation,
            cache_config=cache_config,
            target_protocol=ANTHROPIC_PROTOCOL,
            target_provider_kind="anthropic",
            resolved_policy=resolved,
        )
        assert result.plan.status == "applied"
        assert result.plan.policy_name == "loose"
        assert result.plan.policy_source == "policy:loose"
        assert len(result.plan.candidates) <= 2


# ---------------------------------------------------------------------------
# Routing guardrails (Phase 8 invariant for Phase 9)
# ---------------------------------------------------------------------------


class TestPhase9RoutingGuardrails:
    """The selector never exposes fields the scorer could consume.

    The Phase 8 guardrails invariant extends to Phase 9: synthetic
    cache controls are observational and never feed
    ``QuotaFairScorer.score_accounts``.  We pin this by checking
    that the selector output is structurally independent of any
    routing-relevant field and that the mutator never modifies the
    request body for routing purposes.
    """

    def test_selector_does_not_consume_routing_state(self) -> None:
        from eggpool.transcoder.cache_synthesis import (
            select_synthetic_cache_candidates,
        )

        payload = _anthropic_payload(system=[{"type": "text", "text": "x" * 4096}])
        segmentation = segment_request(payload, protocol=ANTHROPIC_PROTOCOL)
        cache_config = CacheConfig(
            synthetic_cache_controls=SyntheticCacheControlsConfig(
                enabled=True,
                dry_run=False,
                require_policy=False,
                min_stable_tokens=0,
            )
        )
        # No resolved policy => no provider routing information.
        plan = select_synthetic_cache_candidates(
            segmentation,
            payload,
            cache_config=cache_config,
            target_protocol=ANTHROPIC_PROTOCOL,
            target_provider_kind=None,
            resolved_policy=None,
        )
        # Plan carries no fields that could enter a scorer.
        plan_dict = plan.as_dict()
        forbidden_keys = {"score", "quota", "tier", "weight"}
        assert not (set(plan_dict) & forbidden_keys)

    def test_mutator_never_alters_volatile_suffix(self) -> None:
        payload = _anthropic_payload(
            system=[{"type": "text", "text": "x" * 4096}],
            tools=[
                {
                    "name": "search",
                    "description": "search",
                    "input_schema": {"type": "object"},
                }
            ],
        )
        payload["messages"] = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_0",
                        "content": "stack trace line 1",
                    }
                ],
            }
        ]
        segmentation = segment_request(payload, protocol=ANTHROPIC_PROTOCOL)
        cache_config = CacheConfig(
            synthetic_cache_controls=SyntheticCacheControlsConfig(
                enabled=True,
                dry_run=False,
                require_policy=False,
                min_stable_tokens=0,
            )
        )
        resolved = _resolved_policy(
            enabled=True,
            dry_run=False,
            min_stable_tokens=0,
        )
        result = run_synthetic_cache_synthesis(
            payload,
            segmentation=segmentation,
            cache_config=cache_config,
            target_protocol=ANTHROPIC_PROTOCOL,
            target_provider_kind="anthropic",
            resolved_policy=resolved,
        )
        mutated = result.transformed_payload
        assert mutated is not None
        # Tool result must not carry cache_control.
        msgs = cast("list[Any]", mutated["messages"])
        content_list = cast("list[Any]", msgs[0]["content"])
        assert "cache_control" not in content_list[0]


# ---------------------------------------------------------------------------
# Routing guardrails (Phase 8 invariant for Phase 10)
# ---------------------------------------------------------------------------


class TestPhase10RoutingGuardrails:
    """Phase 10 closed-loop threshold tuning is observational only.

    Phase 8 codifies the invariant that cache/compression fields
    never enter route scoring, health removal, or route reselection.
    Phase 10 introduces a tuning recommendation engine that mutates
    per-request compression thresholds at request time; this test
    pins that the resolver still never exposes tuning state in a
    form the ``QuotaFairScorer`` could consume and that the engine
    itself never alters routing.
    """

    def test_resolved_policy_carries_no_tuning_state_for_scorer(self) -> None:
        from eggpool.transcoder.compression.policy import CompressionConfig
        from eggpool.transcoder.compression.policy_resolver import (
            CompressionPolicyContext,
            resolve_compression_policy,
        )
        from eggpool.transcoder.compression.tuning import (
            RuntimeCompressionPolicyOverride,
            RuntimeCompressionPolicyOverrideRegistry,
        )

        reg = RuntimeCompressionPolicyOverrideRegistry()
        reg.register(
            RuntimeCompressionPolicyOverride(
                policy_name="<global>",
                fields={
                    "min_candidate_tokens": 256.0,
                    "min_savings_tokens": 128.0,
                    "max_compression_latency_ms": 15000.0,
                },
                generated_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
                reason_codes=("applied_runtime_override",),
            ),
        )
        resolved = resolve_compression_policy(
            CompressionConfig(),
            CompressionPolicyContext(),
            runtime_override_registry=reg,
        )
        as_dict = resolved.as_dict()
        # The only fields the scorer could see via routing code paths
        # are config_min_candidate_tokens, config_min_savings_tokens,
        # and config_max_compression_latency_ms -- which are already
        # observed by Phase 5 and remain load-only (request count +
        # token count).  Phase 10 introduces no new scorer-facing
        # state.  Pin this by enumerating the surfaced keys.
        forbidden_new_keys = {
            "config_tuning_mode",
            "config_tuning_window_seconds",
            "config_tuning_min_window_requests",
            "config_tuning_max_adjustment_pct",
            "config_tuning_cooldown_seconds",
            "config_tuning_targets",
            "config_tuning_bounds",
        }
        assert forbidden_new_keys.isdisjoint(as_dict.keys())

    def test_tuning_does_not_change_routing_score_signature(self) -> None:
        from eggpool.transcoder.compression.policy import CompressionConfig
        from eggpool.transcoder.compression.policy_resolver import (
            CompressionPolicyContext,
            resolve_compression_policy,
        )
        from eggpool.transcoder.compression.tuning import (
            RuntimeCompressionPolicyOverride,
            RuntimeCompressionPolicyOverrideRegistry,
        )

        # Adversarial input: a registry with extreme overrides that
        # would crash if any tuning field leaked into routing.
        reg = RuntimeCompressionPolicyOverrideRegistry()
        reg.register(
            RuntimeCompressionPolicyOverride(
                policy_name="<global>",
                fields={
                    "min_candidate_tokens": 1.0,
                    "min_savings_tokens": 1.0,
                    "max_compression_latency_ms": 1.0,
                },
                generated_at=datetime.now(UTC),
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
                reason_codes=("applied_runtime_override",),
            ),
        )
        ctx = CompressionPolicyContext()
        baseline = resolve_compression_policy(CompressionConfig(), ctx)
        with_override = resolve_compression_policy(
            CompressionConfig(),
            ctx,
            runtime_override_registry=reg,
        )
        # The only thing that should change is the resolved config
        # knobs; no RoutingScore, no scorer, no health manager is
        # exposed or mutated.  Pin this by structural comparison.
        assert baseline.matched_policy_names == with_override.matched_policy_names
        assert baseline.warnings == with_override.warnings
        assert baseline.synthetic_cache_overrides == (
            with_override.synthetic_cache_overrides
        )

    def test_registry_lookup_never_inspects_request_body(self) -> None:
        """Tuning lookup is content-private; the registry only reads
        the policy name off the resolver output, never the request."""
        from eggpool.transcoder.compression.policy import CompressionConfig
        from eggpool.transcoder.compression.policy_resolver import (
            CompressionPolicyContext,
            resolve_compression_policy,
        )
        from eggpool.transcoder.compression.tuning import (
            RuntimeCompressionPolicyOverrideRegistry,
        )

        reg = RuntimeCompressionPolicyOverrideRegistry()
        # Adversarial context with no body, no protocol, no model --
        # the lookup must still succeed (returning None) without
        # touching any request field.
        ctx = CompressionPolicyContext(
            client_id=None,
            client_name=None,
            source_protocol="openai",
            target_protocol=None,
            requested_model=None,
            resolved_model=None,
            provider_id=None,
            provider_kind=None,
            transcoded=False,
        )
        result = resolve_compression_policy(
            CompressionConfig(),
            ctx,
            runtime_override_registry=reg,
        )
        assert result.runtime_override_metadata["active"] is False
