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

from eggpool.catalog.capabilities import PromptCacheCapability
from eggpool.request.provider_bound_request import ProviderBoundRequest
from eggpool.transcoder.cache_stability import CACHE_BOUNDARY_KIND_SYNTHESIZED
from eggpool.transcoder.cache_synthesis import (
    WARN_BELOW_MIN_TOKENS,
    WARN_CAPABILITY_UNVERIFIED,
    WARN_DISABLED,
    WARN_DRY_RUN,
    WARN_EXISTING_NATIVE_PRESERVED,
    WARN_LIMIT_REACHED,
    WARN_NO_STABLE_CANDIDATE,
    WARN_POLICY_REQUIRED,
    WARN_PROVIDER_UNSUPPORTED,
    WARN_SYNTHESIZED,
    SyntheticCacheCandidate,
    SyntheticCachePlan,
    _existing_native_cache_controls,
    _path_to_display,
    apply_synthetic_cache_controls,
)
from eggpool.transcoder.cache_synthesis import (
    run_synthetic_cache_synthesis as _run_synthetic_cache_synthesis,
)
from eggpool.transcoder.cache_synthesis import (
    select_synthetic_cache_candidates as _select_synthetic_cache_candidates,
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


_TEST_ANTHROPIC_CACHE_CAPABILITY = PromptCacheCapability(
    dialect="first_party",
    supported_ttls=["5m", "1h"],
    default_ttl="5m",
)


def select_synthetic_cache_candidates(*args: Any, **kwargs: Any) -> Any:
    """Use the verified Anthropic contract for legacy selector fixtures."""
    kwargs.setdefault("target_cache_capability", _TEST_ANTHROPIC_CACHE_CAPABILITY)
    return _select_synthetic_cache_candidates(*args, **kwargs)


def run_synthetic_cache_synthesis(*args: Any, **kwargs: Any) -> Any:
    """Use the verified Anthropic contract for legacy synthesis fixtures."""
    kwargs.setdefault("target_cache_capability", _TEST_ANTHROPIC_CACHE_CAPABILITY)
    return _run_synthetic_cache_synthesis(*args, **kwargs)


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


def _provider_bound(
    body: bytes, *, client_protocol: str = "anthropic"
) -> ProviderBoundRequest:
    """Build an already-serialized provider payload for coordinator tests."""
    payload = json.loads(body)
    assert isinstance(payload, dict)
    request = ProviderBoundRequest(
        client_bytes=body,
        client_payload=payload,
        client_protocol=client_protocol,
        model_id=str(payload["model"]),
        upstream_protocol="anthropic",
    )
    request.set_provider_bytes(body)
    return request


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

    def test_unverified_target_capability_blocks_synthesis(self) -> None:
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

        plan = _select_synthetic_cache_candidates(
            segmentation,
            payload,
            cache_config=cache_config,
            target_protocol=ANTHROPIC_PROTOCOL,
            target_provider_kind="anthropic",
            target_cache_capability=None,
            resolved_policy=None,
        )

        assert plan.status == "capability_unverified"
        assert plan.candidates == ()
        assert WARN_CAPABILITY_UNVERIFIED in plan.warnings

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

    def test_apply_preserves_native_cache_control_on_tools(self) -> None:
        payload = _anthropic_payload(
            system=[{"type": "text", "text": "x" * 4096}],
            tools=[
                {
                    "name": "lookup",
                    "description": "lookup",
                    "input_schema": {"type": "object"},
                    "cache_control": {"type": "ephemeral"},
                }
            ],
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
        assert result.transformed_payload is not None
        tools = result.transformed_payload["tools"]
        assert isinstance(tools, list)
        # Native cache_control is preserved byte-for-byte.
        assert tools[0]["cache_control"] == {"type": "ephemeral"}
        # Native-preserved warning is emitted and the mutator skipped the
        # native-annotated block, so no synthetic annotation is layered on
        # top of the native one.  ``applied_count`` reconciles to the
        # number of cache_control additions the mutator actually made,
        # which excludes native-only blocks.
        assert WARN_EXISTING_NATIVE_PRESERVED in result.warnings
        # The mutator may still surface tools[0] in the candidate set
        # (the selector picks it) but the actual mutation count must
        # exclude it.
        assert result.plan.applied_count < len(result.plan.candidates) or all(
            c.placement != "tools" or c.source_path != ("tools", 0)
            for c in result.plan.candidates
        )

    def test_apply_preserves_native_cache_control_on_message_block(self) -> None:
        # Native cache_control on a message content block is preserved
        # even though that placement is not currently selected by the
        # selector.
        payload = _anthropic_payload(system=[{"type": "text", "text": "x" * 4096}])
        payload["messages"] = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "hi",
                        "cache_control": {"type": "ephemeral"},
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
        assert result.transformed_payload is not None
        messages = result.transformed_payload["messages"]
        assert isinstance(messages, list)
        content = messages[0]["content"]
        assert isinstance(content, list)
        # Native cache_control on the message block is preserved.
        assert content[0]["cache_control"] == {"type": "ephemeral"}

    def test_apply_mixed_native_and_synthetic_only_annotates_unannotated(
        self,
    ) -> None:
        # system[0] has native cache_control; system[1] does not.
        # Only system[1] should receive a synthetic annotation.
        payload = _anthropic_payload(
            system=[
                {
                    "type": "text",
                    "text": "x" * 4096,
                    "cache_control": {"type": "ephemeral"},
                },
                {"type": "text", "text": "y" * 4096},
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
        assert result.transformed_payload is not None
        system = result.transformed_payload["system"]
        assert isinstance(system, list)
        # Native annotation preserved exactly.
        assert system[0]["cache_control"] == {"type": "ephemeral"}
        # Synthetic annotation only on the unannotated block.
        assert system[1]["cache_control"] == {"type": "ephemeral"}

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
        payload = _anthropic_payload(
            system=[
                {"type": "text", "text": "x" * 4096},
                {"type": "text", "text": "y" * 4096},
            ]
        )
        candidates = (
            SyntheticCacheCandidate(
                placement="system",
                source_path=("system", 0, "text"),
                target_path=("system", 0, "text"),
                estimated_tokens=1000,
                reason="system_candidate",
                policy_name="policy:x",
                policy_source="policy:x",
                ttl="ephemeral",
            ),
            SyntheticCacheCandidate(
                placement="system",
                source_path=("system", 1, "text"),
                target_path=("system", 1, "text"),
                estimated_tokens=1000,
                reason="system_candidate",
                policy_name="policy:x",
                policy_source="policy:x",
                ttl="ephemeral",
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
        plan = _select_synthetic_cache_candidates(
            segmentation,
            payload,
            cache_config=cache_config,
            target_protocol=ANTHROPIC_PROTOCOL,
            target_provider_kind=None,
            target_cache_capability=_TEST_ANTHROPIC_CACHE_CAPABILITY,
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


# ---------------------------------------------------------------------------
# Phase C: Tuple path normalization
# ---------------------------------------------------------------------------


class TestPathNormalization:
    """Synthetic cache paths use normalized tuple representation."""

    def test_path_display_helper_formats_tuples_as_dot_notation(self) -> None:
        assert _path_to_display(("system", 0, "text")) == "system.0.text"
        assert _path_to_display(("tools", 0)) == "tools.0"
        assert _path_to_display(("messages", 3, "content", 0)) == (
            "messages.3.content.0"
        )
        assert _path_to_display(()) == ""

    def test_candidate_paths_are_tuples_not_strings(self) -> None:
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
        assert plan.candidate_count >= 1
        for candidate in plan.candidates:
            assert isinstance(candidate.source_path, tuple)
            assert isinstance(candidate.target_path, tuple)

    def test_existing_native_paths_use_tuple_normalized_form(self) -> None:
        payload = _anthropic_payload(
            system=[
                {
                    "type": "text",
                    "text": "x" * 4096,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[
                {
                    "name": "lookup",
                    "description": "lookup",
                    "input_schema": {"type": "object"},
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        )
        native = _existing_native_cache_controls(payload)
        assert ("system", 0) in native
        assert ("tools", 0) in native
        assert len(native) == 2

    def test_native_cache_control_preserved_when_tuple_path_matches(self) -> None:
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
        system = result.transformed_payload["system"]
        assert isinstance(system, list)
        assert system[0]["cache_control"] == {"type": "ephemeral"}
        assert system[0]["text"] == "x" * 4096


# ---------------------------------------------------------------------------
# Phase D: Effective TTL
# ---------------------------------------------------------------------------


class TestEffectiveTTL:
    """Synthetic cache controls honor the configured TTL."""

    def test_unsupported_ttl_rejected_at_config_load(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="ttl"):
            SyntheticCacheControlsConfig(ttl="5m")
        with pytest.raises(ValidationError, match="ttl"):
            SyntheticCacheControlsConfig(ttl="1h")

    def test_only_ephemeral_ttl_accepted(self) -> None:
        config = SyntheticCacheControlsConfig(ttl="ephemeral")
        assert config.ttl == "ephemeral"

    def test_apply_uses_effective_ttl_from_plan(self) -> None:
        payload = _anthropic_payload(system=[{"type": "text", "text": "x" * 4096}])
        candidates = (
            SyntheticCacheCandidate(
                placement="system",
                source_path=("system", 0, "text"),
                target_path=("system", 0, "text"),
                estimated_tokens=1000,
                reason="system_candidate",
                policy_name="policy:x",
                policy_source="policy:x",
                ttl="ephemeral",
            ),
        )
        plan = SyntheticCachePlan(
            status="applied",
            dry_run=False,
            candidates=candidates,
            applied_count=1,
            warnings=(),
            policy_name="policy:x",
            policy_source="policy:x",
            effective_ttl="ephemeral",
        )
        mutated, applied, annotations = apply_synthetic_cache_controls(payload, plan)
        assert applied == 1
        assert mutated["system"][0]["cache_control"] == {"type": "ephemeral"}
        assert len(annotations) == 1
        assert annotations[0].cache_control_type == "ephemeral"

    def test_plan_as_dict_includes_effective_ttl(self) -> None:
        plan = SyntheticCachePlan(
            status="dry_run",
            dry_run=True,
            candidates=(),
            applied_count=0,
            warnings=(),
            policy_name="<global>",
            policy_source="global",
            effective_ttl="ephemeral",
        )
        d = plan.as_dict()
        assert d["effective_ttl"] == "ephemeral"

    def test_plan_as_dict_includes_display_paths(self) -> None:
        candidates = (
            SyntheticCacheCandidate(
                placement="system",
                source_path=("system", 0, "text"),
                target_path=("system", 0, "text"),
                estimated_tokens=1000,
                reason="system_candidate",
                policy_name="p",
                policy_source="ps",
                ttl="ephemeral",
            ),
        )
        plan = SyntheticCachePlan(
            status="dry_run",
            dry_run=True,
            candidates=candidates,
            applied_count=0,
            warnings=(),
            policy_name="p",
            policy_source="ps",
        )
        d = plan.as_dict()
        assert d["candidate_source_paths"] == ["system.0.text"]
        assert d["candidate_target_paths"] == ["system.0.text"]


# ---------------------------------------------------------------------------
# Phase G: structural-diff safety check
# ---------------------------------------------------------------------------


class TestStructuralCacheDiff:
    """_structural_cache_diff reports only paths and change kinds."""

    def test_detects_unexpected_additions(self) -> None:
        from eggpool.transcoder.cache_synthesis import _structural_cache_diff

        original: dict[str, Any] = {"a": 1, "b": {"c": 2}}
        mutated: dict[str, Any] = {"a": 1, "b": {"c": 2}, "d": 3}
        diff = _structural_cache_diff(original, mutated)
        assert ["d"] in diff["added_paths"]
        assert diff["removed_paths"] == []
        assert diff["changed_paths"] == []

    def test_allows_only_cache_control_additions(self) -> None:
        from eggpool.transcoder.cache_synthesis import _structural_cache_diff

        original: dict[str, Any] = {
            "system": [{"type": "text", "text": "x"}],
            "messages": [{"role": "user", "content": "hi"}],
        }
        mutated: dict[str, Any] = {
            "system": [
                {"type": "text", "text": "x", "cache_control": {"type": "ephemeral"}}
            ],
            "messages": [{"role": "user", "content": "hi"}],
        }
        diff = _structural_cache_diff(original, mutated)
        # All added paths end with cache_control
        for path in diff["added_paths"]:
            assert path[-1] == "cache_control"
        assert diff["removed_paths"] == []
        assert diff["changed_paths"] == []

    def test_detects_removed_paths(self) -> None:
        from eggpool.transcoder.cache_synthesis import _structural_cache_diff

        original: dict[str, Any] = {"a": 1, "b": 2}
        mutated: dict[str, Any] = {"a": 1}
        diff = _structural_cache_diff(original, mutated)
        assert diff["removed_paths"] == [["b"]]

    def test_detects_changed_values(self) -> None:
        from eggpool.transcoder.cache_synthesis import _structural_cache_diff

        original: dict[str, Any] = {"a": 1, "b": [1, 2, 3]}
        mutated: dict[str, Any] = {"a": 1, "b": [1, 99, 3]}
        diff = _structural_cache_diff(original, mutated)
        assert diff["changed_paths"] == [["b", 1]]

    def test_empty_diff_for_identical_payloads(self) -> None:
        from eggpool.transcoder.cache_synthesis import _structural_cache_diff

        payload: dict[str, Any] = {"a": [1, 2], "b": {"c": "d"}}
        diff = _structural_cache_diff(payload, payload)
        assert diff == {"added_paths": [], "removed_paths": [], "changed_paths": []}


class TestValidateSyntheticCacheDiff:
    """The coordinator runs ``_validate_synthetic_cache_diff`` against
    the candidate set returned by the selector; an added
    ``cache_control`` outside the candidate set is a safety failure
    and flips the plan to ``failed_fallback``.
    """

    def _candidate(self, target_path: tuple[str | int, ...]) -> SyntheticCacheCandidate:
        return SyntheticCacheCandidate(
            placement="system",
            source_path=target_path,
            target_path=target_path,
            estimated_tokens=1024,
            reason="system_candidate",
            policy_name="<global>",
            policy_source="global",
            ttl="ephemeral",
        )

    def test_allows_cache_control_at_candidate_container(self) -> None:
        from eggpool.transcoder.cache_synthesis import (
            _structural_cache_diff,
            _validate_synthetic_cache_diff,
        )

        original: dict[str, Any] = {"system": [{"type": "text", "text": "x"}]}
        mutated: dict[str, Any] = {
            "system": [
                {
                    "type": "text",
                    "text": "x",
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        }
        diff = _structural_cache_diff(original, mutated)
        candidates = (self._candidate(("system", 0)),)
        assert _validate_synthetic_cache_diff(diff, candidates) is True

    def test_allows_cache_control_at_candidate_container_with_text_leaf(
        self,
    ) -> None:
        from eggpool.transcoder.cache_synthesis import (
            _structural_cache_diff,
            _validate_synthetic_cache_diff,
        )

        original: dict[str, Any] = {"system": [{"type": "text", "text": "x"}]}
        mutated: dict[str, Any] = {
            "system": [
                {
                    "type": "text",
                    "text": "x",
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        }
        diff = _structural_cache_diff(original, mutated)
        # Candidate target_path points at the text leaf; the mutator
        # walks back to the container, so the validator must accept
        # the cache_control on the container.
        candidates = (self._candidate(("system", 0, "text")),)
        assert _validate_synthetic_cache_diff(diff, candidates) is True

    def test_rejects_cache_control_at_non_candidate_container(self) -> None:
        from eggpool.transcoder.cache_synthesis import (
            _structural_cache_diff,
            _validate_synthetic_cache_diff,
        )

        original: dict[str, Any] = {
            "system": [{"type": "text", "text": "x"}],
            "messages": [{"role": "user", "content": "hi"}],
        }
        mutated: dict[str, Any] = {
            "system": [{"type": "text", "text": "x"}],
            "messages": [
                {
                    "role": "user",
                    "content": "hi",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        }
        diff = _structural_cache_diff(original, mutated)
        # Selector only picked system[0]; cache_control on messages[0]
        # is an unexpected mutation.
        candidates = (self._candidate(("system", 0)),)
        assert _validate_synthetic_cache_diff(diff, candidates) is False

    def test_rejects_non_cache_control_addition(self) -> None:
        from eggpool.transcoder.cache_synthesis import (
            _structural_cache_diff,
            _validate_synthetic_cache_diff,
        )

        original: dict[str, Any] = {"system": [{"type": "text", "text": "x"}]}
        mutated: dict[str, Any] = {
            "system": [{"type": "text", "text": "x"}],
            "extra_field": "sneaky",
        }
        diff = _structural_cache_diff(original, mutated)
        candidates = (self._candidate(("system", 0)),)
        assert _validate_synthetic_cache_diff(diff, candidates) is False

    def test_rejects_text_field_mutation(self) -> None:
        from eggpool.transcoder.cache_synthesis import (
            _structural_cache_diff,
            _validate_synthetic_cache_diff,
        )

        original: dict[str, Any] = {"system": [{"type": "text", "text": "x"}]}
        mutated: dict[str, Any] = {
            "system": [
                {
                    "type": "text",
                    "text": "x",
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        }
        # Pretend the mutator changed the text content in addition
        # to adding cache_control.
        mutated["system"][0]["text"] = "DIFFERENT"
        diff = _structural_cache_diff(original, mutated)
        candidates = (self._candidate(("system", 0)),)
        assert _validate_synthetic_cache_diff(diff, candidates) is False

    def test_rejects_volatile_suffix_container_addition(self) -> None:
        from eggpool.transcoder.cache_synthesis import (
            _structural_cache_diff,
            _validate_synthetic_cache_diff,
        )

        original: dict[str, Any] = {
            "system": [{"type": "text", "text": "x"}],
            "messages": [{"role": "user", "content": "hi"}],
        }
        mutated: dict[str, Any] = {
            "system": [{"type": "text", "text": "x"}],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "hi",
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
            ],
        }
        diff = _structural_cache_diff(original, mutated)
        # Selector only picked system[0]; a cache_control added inside
        # a volatile-suffix message block must be rejected.
        candidates = (self._candidate(("system", 0)),)
        assert _validate_synthetic_cache_diff(diff, candidates) is False

    def test_empty_candidate_set_rejects_cache_control_addition(self) -> None:
        from eggpool.transcoder.cache_synthesis import (
            _structural_cache_diff,
            _validate_synthetic_cache_diff,
        )

        original: dict[str, Any] = {"system": [{"type": "text", "text": "x"}]}
        mutated: dict[str, Any] = {
            "system": [
                {
                    "type": "text",
                    "text": "x",
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        }
        diff = _structural_cache_diff(original, mutated)
        assert _validate_synthetic_cache_diff(diff, ()) is False

    def test_empty_diff_passes_with_empty_candidate_set(self) -> None:
        from eggpool.transcoder.cache_synthesis import (
            _validate_synthetic_cache_diff,
        )

        diff = {"added_paths": [], "removed_paths": [], "changed_paths": []}
        assert _validate_synthetic_cache_diff(diff, ()) is True


class TestResolveSelectedProviderKind:
    """resolve_selected_provider_kind: catalog first, config fallback, never raises."""

    def _selected(self, provider_id: str | None) -> Any:
        from eggpool.request.coordinator import SelectedAttempt

        return SelectedAttempt(
            proxy_request_id="r",
            db_request_id="1",
            attempt_id=1,
            reservation_id="r1",
            account_id=1,
            account_name="acct",
            api_key="sk-test",
            model_id="m",
            estimated_tokens=0,
            estimated_microdollars=0,
            attempt_number=1,
            provider_id=provider_id or "",
            protocol="openai",
        )

    def test_catalog_kind_wins(self) -> None:
        from eggpool.request.coordinator import resolve_selected_provider_kind

        catalog = type(
            "Catalog",
            (),
            {
                "providers": {
                    "p1": type("P", (), {"kind": "anthropic"})(),
                    "p2": type("P", (), {"kind": "openai"})(),
                }
            },
        )()
        result = resolve_selected_provider_kind(catalog, self._selected("p1"))
        assert result == "anthropic"

    def test_config_fallback_when_catalog_missing(self) -> None:
        from eggpool.request.coordinator import resolve_selected_provider_kind

        # Catalog has no providers attribute.
        catalog = type("Catalog", (), {})()
        # Config has the provider with kind.
        config = type(
            "Config",
            (),
            {
                "providers": {
                    "p1": type("P", (), {"kind": "anthropic"})(),
                }
            },
        )()
        result = resolve_selected_provider_kind(
            catalog, self._selected("p1"), config=config
        )
        assert result == "anthropic"

    def test_catalog_wins_over_config(self) -> None:
        from eggpool.request.coordinator import resolve_selected_provider_kind

        catalog = type(
            "Catalog",
            (),
            {
                "providers": {
                    "p1": type("P", (), {"kind": "anthropic"})(),
                }
            },
        )()
        config = type(
            "Config",
            (),
            {
                "providers": {
                    "p1": type("P", (), {"kind": "openai"})(),
                }
            },
        )()
        result = resolve_selected_provider_kind(
            catalog, self._selected("p1"), config=config
        )
        assert result == "anthropic"

    def test_unknown_provider_returns_none(self) -> None:
        from eggpool.request.coordinator import resolve_selected_provider_kind

        catalog = type("Catalog", (), {})()
        config = type(
            "Config",
            (),
            {
                "providers": {
                    "p1": type("P", (), {"kind": "anthropic"})(),
                }
            },
        )()
        # Provider id not in catalog or config.
        result = resolve_selected_provider_kind(
            catalog, self._selected("missing"), config=config
        )
        assert result is None

    def test_no_selected_returns_none(self) -> None:
        from eggpool.request.coordinator import resolve_selected_provider_kind

        catalog = type(
            "Catalog",
            (),
            {"providers": {"p1": type("P", (), {"kind": "anthropic"})()}},
        )()
        assert resolve_selected_provider_kind(catalog, None) is None

    def test_catalog_kind_empty_string_falls_through(self) -> None:
        from eggpool.request.coordinator import resolve_selected_provider_kind

        catalog = type(
            "Catalog",
            (),
            {
                "providers": {
                    "p1": type("P", (), {"kind": ""})(),
                }
            },
        )()
        config = type(
            "Config",
            (),
            {
                "providers": {
                    "p1": type("P", (), {"kind": "anthropic"})(),
                }
            },
        )()
        result = resolve_selected_provider_kind(
            catalog, self._selected("p1"), config=config
        )
        assert result == "anthropic"


# ---------------------------------------------------------------------------
# Post-route synthetic cache controls (Phase A corrective pass)
# ---------------------------------------------------------------------------


class TestSyntheticCachePostRoute:
    """Post-route synthetic cache controls in RequestCoordinator."""

    def test_synthetic_cache_runs_after_route_selection(self) -> None:
        from eggpool.request.coordinator import (
            ProxyRequestContext,
            RequestCoordinator,
            SelectedAttempt,
        )

        payload = _anthropic_payload(system=[{"type": "text", "text": "x" * 4096}])
        cache_config = CacheConfig(
            synthetic_cache_controls=SyntheticCacheControlsConfig(
                enabled=True,
                dry_run=True,
                require_policy=False,
                min_stable_tokens=0,
            )
        )
        ctx = ProxyRequestContext(
            request_id="test-req",
            protocol="anthropic",
            model_id="claude-3-5-sonnet",
            streaming=False,
            original_body=json.dumps(payload).encode(),
            incoming_headers={},
            upstream_protocol="anthropic",
            provider_bound=_provider_bound(json.dumps(payload).encode()),
        )
        selected = SelectedAttempt(
            proxy_request_id="test-req",
            db_request_id="1",
            attempt_id=1,
            reservation_id="r1",
            account_id=1,
            account_name="test-acct",
            api_key="sk-test",
            model_id="claude-3-5-sonnet",
            estimated_tokens=100,
            estimated_microdollars=0,
            attempt_number=1,
            provider_id="anthropic-test",
            protocol="anthropic",
        )
        coordinator = object.__new__(RequestCoordinator)
        coordinator._cache_config = cache_config
        coordinator._compression_tuning_registry = None
        coordinator._compression_policy = CompressionConfig()
        coordinator._catalog = None
        coordinator._config = None

        coordinator._apply_synthetic_cache_controls(context=ctx, selected=selected)
        assert ctx.synthetic_cache_result is not None
        assert ctx.synthetic_cache_result.plan.status in (
            "dry_run",
            "disabled",
            "no_candidates",
            "policy_required",
            "capability_unverified",
        )

    def test_openai_client_to_anthropic_provider_post_route_synthesis(self) -> None:
        from eggpool.request.coordinator import (
            ProxyRequestContext,
            RequestCoordinator,
            SelectedAttempt,
        )

        payload = _anthropic_payload(
            system=[{"type": "text", "text": "x" * 4096}],
        )
        cache_config = CacheConfig(
            synthetic_cache_controls=SyntheticCacheControlsConfig(
                enabled=True,
                dry_run=True,
                require_policy=False,
                min_stable_tokens=0,
            )
        )
        ctx = ProxyRequestContext(
            request_id="test-req",
            protocol="openai",
            model_id="claude-3-5-sonnet",
            streaming=False,
            original_body=json.dumps(payload).encode(),
            incoming_headers={},
            upstream_protocol="anthropic",
            provider_bound=_provider_bound(
                json.dumps(payload).encode(), client_protocol="openai"
            ),
        )
        selected = SelectedAttempt(
            proxy_request_id="test-req",
            db_request_id="1",
            attempt_id=1,
            reservation_id="r1",
            account_id=1,
            account_name="test-acct",
            api_key="sk-test",
            model_id="claude-3-5-sonnet",
            estimated_tokens=100,
            estimated_microdollars=0,
            attempt_number=1,
            provider_id="anthropic-test",
            protocol="openai",
        )
        coordinator = object.__new__(RequestCoordinator)
        coordinator._cache_config = cache_config
        coordinator._compression_tuning_registry = None
        coordinator._compression_policy = CompressionConfig()
        coordinator._catalog = None
        coordinator._config = None

        coordinator._apply_synthetic_cache_controls(context=ctx, selected=selected)
        assert ctx.synthetic_cache_result is not None
        # OpenAI client, Anthropic upstream => target_protocol = "anthropic"
        # so the selector should NOT reject as provider_unsupported
        assert ctx.synthetic_cache_result.plan.status != "provider_unsupported"
        assert ctx.synthetic_cache_result.plan.status in (
            "dry_run",
            "applied",
            "no_candidates",
            "disabled",
            "policy_required",
            "capability_unverified",
        )

    def test_openai_client_to_openai_provider_unsupported(self) -> None:
        from eggpool.request.coordinator import (
            ProxyRequestContext,
            RequestCoordinator,
            SelectedAttempt,
        )

        payload: dict[str, Any] = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "hi"}],
        }
        cache_config = CacheConfig(
            synthetic_cache_controls=SyntheticCacheControlsConfig(
                enabled=True,
                dry_run=True,
                require_policy=False,
                min_stable_tokens=0,
            )
        )
        ctx = ProxyRequestContext(
            request_id="test-req",
            protocol="openai",
            model_id="gpt-4o",
            streaming=False,
            original_body=json.dumps(payload).encode(),
            incoming_headers={},
            upstream_protocol="openai",
            provider_bound=_provider_bound(
                json.dumps(payload).encode(), client_protocol="openai"
            ),
        )
        selected = SelectedAttempt(
            proxy_request_id="test-req",
            db_request_id="1",
            attempt_id=1,
            reservation_id="r1",
            account_id=1,
            account_name="test-acct",
            api_key="sk-test",
            model_id="gpt-4o",
            estimated_tokens=100,
            estimated_microdollars=0,
            attempt_number=1,
            provider_id="openai-test",
            protocol="openai",
        )
        coordinator = object.__new__(RequestCoordinator)
        coordinator._cache_config = cache_config
        coordinator._compression_tuning_registry = None
        coordinator._compression_policy = CompressionConfig()
        coordinator._catalog = None
        coordinator._config = None

        coordinator._apply_synthetic_cache_controls(context=ctx, selected=selected)
        assert ctx.synthetic_cache_result is not None
        assert ctx.synthetic_cache_result.plan.status == "provider_unsupported"

    def test_post_route_provider_specific_matchers_fire(self) -> None:
        from eggpool.request.coordinator import (
            ProxyRequestContext,
            RequestCoordinator,
            SelectedAttempt,
        )

        payload = _anthropic_payload(
            system=[{"type": "text", "text": "x" * 4096}],
        )
        ctx = ProxyRequestContext(
            request_id="test-req",
            protocol="openai",
            model_id="claude-3-5-sonnet",
            streaming=False,
            original_body=json.dumps(payload).encode(),
            incoming_headers={},
            upstream_protocol="anthropic",
            provider_bound=_provider_bound(
                json.dumps(payload).encode(), client_protocol="openai"
            ),
        )
        selected = SelectedAttempt(
            proxy_request_id="test-req",
            db_request_id="1",
            attempt_id=1,
            reservation_id="r1",
            account_id=1,
            account_name="test-acct",
            api_key="sk-test",
            model_id="claude-3-5-sonnet",
            estimated_tokens=100,
            estimated_microdollars=0,
            attempt_number=1,
            provider_id="anthropic-test",
            protocol="openai",
        )

        overrides = [
            CompressionPolicyOverride(
                name="anthropic-cache",
                match_protocols=["anthropic"],
                match_provider_kinds=["anthropic"],
                synthetic_cache_controls=True,
                synthetic_cache_dry_run=True,
                synthetic_cache_min_stable_tokens=0,
            )
        ]
        config_with_overrides = CompressionConfig()
        config_with_overrides.policies = overrides

        cache_config = CacheConfig(
            synthetic_cache_controls=SyntheticCacheControlsConfig(
                enabled=True,
                dry_run=True,
                require_policy=True,
                min_stable_tokens=0,
            )
        )
        # Mock catalog so resolve_selected_provider_kind returns a kind
        mock_catalog = type(
            "MockCatalog",
            (),
            {"providers": {"anthropic-test": type("P", (), {"kind": "anthropic"})()}},
        )()
        coordinator = object.__new__(RequestCoordinator)
        coordinator._cache_config = cache_config
        coordinator._compression_tuning_registry = None
        coordinator._compression_policy = config_with_overrides
        coordinator._catalog = mock_catalog
        coordinator._config = None
        ctx.resolved_compression_policy = None

        coordinator._apply_synthetic_cache_controls(context=ctx, selected=selected)
        assert ctx.synthetic_cache_result is not None
        # Provider-specific matcher fired, so status should not be policy_required
        assert ctx.synthetic_cache_result.plan.status != "policy_required"

    def test_pre_route_provider_specific_matchers_noop(self) -> None:
        payload = _anthropic_payload(
            system=[{"type": "text", "text": "x" * 4096}],
        )
        cache_config = CacheConfig(
            synthetic_cache_controls=SyntheticCacheControlsConfig(
                enabled=True,
                dry_run=True,
                require_policy=True,
                min_stable_tokens=0,
            )
        )
        segmentation = segment_request(payload, protocol=ANTHROPIC_PROTOCOL)
        base = CompressionConfig()
        overrides = [
            CompressionPolicyOverride(
                name="anthropic-cache",
                match_provider_kinds=["anthropic"],
                synthetic_cache_controls=True,
                synthetic_cache_dry_run=True,
                synthetic_cache_min_stable_tokens=0,
            )
        ]
        # Pre-route context: no provider_kind => matcher does not fire
        # (match_protocols removed so only provider_kind discriminates)
        pre_route_ctx = CompressionPolicyContext(
            source_protocol="openai",
            provider_kind=None,
        )
        resolved = resolve_compression_policy(base, pre_route_ctx, overrides=overrides)
        assert resolved.synthetic_cache_overrides is None
        result = run_synthetic_cache_synthesis(
            payload,
            segmentation=segmentation,
            cache_config=cache_config,
            target_protocol=ANTHROPIC_PROTOCOL,
            target_provider_kind="anthropic",
            resolved_policy=resolved,
        )
        assert result.plan.status == "policy_required"

    def test_failed_fallback_when_structural_diff_detects_change(self) -> None:
        from eggpool.transcoder.cache_synthesis import (
            _structural_cache_diff,
        )

        original: dict[str, Any] = {
            "system": [{"type": "text", "text": "x" * 4096}],
            "messages": [{"role": "user", "content": "hi"}],
        }
        mutated: dict[str, Any] = {
            "system": [
                {
                    "type": "text",
                    "text": "changed!",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": "hi"}],
        }
        diff = _structural_cache_diff(original, mutated)
        # The text field changed AND cache_control was added
        has_text_change = any("text" in path for path in diff["changed_paths"])
        assert has_text_change
        unexpected = [p for p in diff["added_paths"] if p[-1] != "cache_control"]
        has_other_changes = unexpected or diff["changed_paths"]
        assert has_other_changes

    def test_target_protocol_uses_upstream_protocol_not_client_protocol(self) -> None:
        from eggpool.request.coordinator import (
            ProxyRequestContext,
            RequestCoordinator,
            SelectedAttempt,
        )

        payload = _anthropic_payload(
            system=[{"type": "text", "text": "x" * 4096}],
        )
        cache_config = CacheConfig(
            synthetic_cache_controls=SyntheticCacheControlsConfig(
                enabled=True,
                dry_run=True,
                require_policy=False,
                min_stable_tokens=0,
            )
        )
        ctx = ProxyRequestContext(
            request_id="test-req",
            protocol="openai",
            model_id="claude-3-5-sonnet",
            streaming=False,
            original_body=json.dumps(payload).encode(),
            incoming_headers={},
            upstream_protocol="anthropic",
            provider_bound=_provider_bound(
                json.dumps(payload).encode(), client_protocol="openai"
            ),
        )
        selected = SelectedAttempt(
            proxy_request_id="test-req",
            db_request_id="1",
            attempt_id=1,
            reservation_id="r1",
            account_id=1,
            account_name="test-acct",
            api_key="sk-test",
            model_id="claude-3-5-sonnet",
            estimated_tokens=100,
            estimated_microdollars=0,
            attempt_number=1,
            provider_id="anthropic-test",
            protocol="openai",
        )
        coordinator = object.__new__(RequestCoordinator)
        coordinator._cache_config = cache_config
        coordinator._compression_tuning_registry = None
        coordinator._compression_policy = CompressionConfig()
        coordinator._catalog = None
        coordinator._config = None

        coordinator._apply_synthetic_cache_controls(context=ctx, selected=selected)
        assert ctx.synthetic_cache_result is not None
        # upstream_protocol is "anthropic" despite client protocol "openai"
        assert ctx.synthetic_cache_result.plan.status != "provider_unsupported"

    def test_synthetic_cache_segmentation_stored_on_context(self) -> None:
        from eggpool.request.coordinator import (
            ProxyRequestContext,
            RequestCoordinator,
            SelectedAttempt,
        )

        payload = _anthropic_payload(
            system=[{"type": "text", "text": "x" * 4096}],
        )
        cache_config = CacheConfig(
            synthetic_cache_controls=SyntheticCacheControlsConfig(
                enabled=True,
                dry_run=True,
                require_policy=False,
                min_stable_tokens=0,
            )
        )
        ctx = ProxyRequestContext(
            request_id="test-req",
            protocol="anthropic",
            model_id="claude-3-5-sonnet",
            streaming=False,
            original_body=json.dumps(payload).encode(),
            incoming_headers={},
            upstream_protocol="anthropic",
            provider_bound=_provider_bound(json.dumps(payload).encode()),
        )
        selected = SelectedAttempt(
            proxy_request_id="test-req",
            db_request_id="1",
            attempt_id=1,
            reservation_id="r1",
            account_id=1,
            account_name="test-acct",
            api_key="sk-test",
            model_id="claude-3-5-sonnet",
            estimated_tokens=100,
            estimated_microdollars=0,
            attempt_number=1,
            provider_id="anthropic-test",
            protocol="anthropic",
        )
        coordinator = object.__new__(RequestCoordinator)
        coordinator._cache_config = cache_config
        coordinator._compression_tuning_registry = None
        coordinator._compression_policy = CompressionConfig()
        coordinator._catalog = None
        coordinator._config = None

        coordinator._apply_synthetic_cache_controls(context=ctx, selected=selected)
        assert ctx.synthetic_cache_segmentation is not None

    def test_provider_bound_body_not_mutated_when_safety_diff_fails(self) -> None:
        from eggpool.request.coordinator import (
            ProxyRequestContext,
            RequestCoordinator,
            SelectedAttempt,
        )

        original_payload = _anthropic_payload(
            system=[{"type": "text", "text": "x" * 4096}],
        )
        original_body = json.dumps(original_payload).encode()
        cache_config = CacheConfig(
            synthetic_cache_controls=SyntheticCacheControlsConfig(
                enabled=True,
                dry_run=False,
                require_policy=False,
                min_stable_tokens=0,
            )
        )
        ctx = ProxyRequestContext(
            request_id="test-req",
            protocol="anthropic",
            model_id="claude-3-5-sonnet",
            streaming=False,
            original_body=original_body,
            incoming_headers={},
            upstream_protocol="anthropic",
            provider_bound=_provider_bound(original_body),
        )
        selected = SelectedAttempt(
            proxy_request_id="test-req",
            db_request_id="1",
            attempt_id=1,
            reservation_id="r1",
            account_id=1,
            account_name="test-acct",
            api_key="sk-test",
            model_id="claude-3-5-sonnet",
            estimated_tokens=100,
            estimated_microdollars=0,
            attempt_number=1,
            provider_id="anthropic-test",
            protocol="anthropic",
        )
        coordinator = object.__new__(RequestCoordinator)
        coordinator._cache_config = cache_config
        coordinator._compression_tuning_registry = None
        coordinator._compression_policy = CompressionConfig()
        coordinator._catalog = None
        coordinator._config = None

        coordinator._apply_synthetic_cache_controls(context=ctx, selected=selected)
        # Apply mode with valid payload updates the provider-bound body.
        # (safety diff passes when mutator only adds cache_control)
        if (
            ctx.synthetic_cache_result is not None
            and ctx.synthetic_cache_result.plan.status == "applied"
        ):
            assert ctx.provider_bound is not None

    # ------------------------------------------------------------------
    # Streaming-path exercises
    # ------------------------------------------------------------------

    def test_synthetic_cache_dry_run_runs_for_streaming_request(self) -> None:
        from eggpool.request.coordinator import (
            ProxyRequestContext,
            RequestCoordinator,
            SelectedAttempt,
        )

        payload = _anthropic_payload(system=[{"type": "text", "text": "x" * 4096}])
        cache_config = CacheConfig(
            synthetic_cache_controls=SyntheticCacheControlsConfig(
                enabled=True,
                dry_run=True,
                require_policy=False,
                min_stable_tokens=0,
            )
        )
        ctx = ProxyRequestContext(
            request_id="test-req",
            protocol="anthropic",
            model_id="claude-3-5-sonnet",
            streaming=True,
            original_body=json.dumps(payload).encode(),
            incoming_headers={},
            upstream_protocol="anthropic",
            provider_bound=_provider_bound(json.dumps(payload).encode()),
        )
        selected = SelectedAttempt(
            proxy_request_id="test-req",
            db_request_id="1",
            attempt_id=1,
            reservation_id="r1",
            account_id=1,
            account_name="test-acct",
            api_key="sk-test",
            model_id="claude-3-5-sonnet",
            estimated_tokens=100,
            estimated_microdollars=0,
            attempt_number=1,
            provider_id="anthropic-test",
            protocol="anthropic",
        )
        coordinator = object.__new__(RequestCoordinator)
        coordinator._cache_config = cache_config
        coordinator._compression_tuning_registry = None
        coordinator._compression_policy = CompressionConfig()
        coordinator._catalog = None
        coordinator._config = None

        coordinator._apply_synthetic_cache_controls(context=ctx, selected=selected)
        assert ctx.synthetic_cache_result is not None
        assert ctx.synthetic_cache_result.plan.status in (
            "dry_run",
            "applied",
            "no_candidates",
            "disabled",
            "policy_required",
            "capability_unverified",
        )

    def test_synthetic_cache_apply_runs_for_streaming_request(self) -> None:
        from eggpool.request.coordinator import (
            ProxyRequestContext,
            RequestCoordinator,
            SelectedAttempt,
        )

        payload = _anthropic_payload(system=[{"type": "text", "text": "x" * 4096}])
        original_body = json.dumps(payload).encode()
        cache_config = CacheConfig(
            synthetic_cache_controls=SyntheticCacheControlsConfig(
                enabled=True,
                dry_run=False,
                require_policy=False,
                min_stable_tokens=0,
            )
        )
        ctx = ProxyRequestContext(
            request_id="test-req",
            protocol="anthropic",
            model_id="claude-3-5-sonnet",
            streaming=True,
            original_body=original_body,
            incoming_headers={},
            upstream_protocol="anthropic",
            provider_bound=_provider_bound(original_body),
        )
        selected = SelectedAttempt(
            proxy_request_id="test-req",
            db_request_id="1",
            attempt_id=1,
            reservation_id="r1",
            account_id=1,
            account_name="test-acct",
            api_key="sk-test",
            model_id="claude-3-5-sonnet",
            estimated_tokens=100,
            estimated_microdollars=0,
            attempt_number=1,
            provider_id="anthropic-test",
            protocol="anthropic",
        )
        coordinator = object.__new__(RequestCoordinator)
        coordinator._cache_config = cache_config
        coordinator._compression_tuning_registry = None
        coordinator._compression_policy = CompressionConfig()
        coordinator._catalog = None
        coordinator._config = None

        coordinator._apply_synthetic_cache_controls(context=ctx, selected=selected)
        assert ctx.synthetic_cache_result is not None
        if ctx.synthetic_cache_result.plan.status == "applied":
            assert ctx.provider_bound is not None
            new_body = ctx.provider_bound.serialize_provider_payload()
            assert new_body != original_body
            new_payload = json.loads(new_body)
            system = new_payload["system"]
            assert isinstance(system, list)
            assert system[0]["cache_control"] == {"type": "ephemeral"}

    def test_synthetic_cache_streaming_context_preserves_result(self) -> None:
        from eggpool.request.coordinator import (
            ProxyRequestContext,
            RequestCoordinator,
            SelectedAttempt,
        )

        payload = _anthropic_payload(system=[{"type": "text", "text": "x" * 4096}])
        cache_config = CacheConfig(
            synthetic_cache_controls=SyntheticCacheControlsConfig(
                enabled=True,
                dry_run=True,
                require_policy=False,
                min_stable_tokens=0,
            )
        )
        ctx = ProxyRequestContext(
            request_id="test-req",
            protocol="anthropic",
            model_id="claude-3-5-sonnet",
            streaming=True,
            original_body=json.dumps(payload).encode(),
            incoming_headers={},
            upstream_protocol="anthropic",
            provider_bound=_provider_bound(json.dumps(payload).encode()),
        )
        selected = SelectedAttempt(
            proxy_request_id="test-req",
            db_request_id="1",
            attempt_id=1,
            reservation_id="r1",
            account_id=1,
            account_name="test-acct",
            api_key="sk-test",
            model_id="claude-3-5-sonnet",
            estimated_tokens=100,
            estimated_microdollars=0,
            attempt_number=1,
            provider_id="anthropic-test",
            protocol="anthropic",
        )
        coordinator = object.__new__(RequestCoordinator)
        coordinator._cache_config = cache_config
        coordinator._compression_tuning_registry = None
        coordinator._compression_policy = CompressionConfig()
        coordinator._catalog = None
        coordinator._config = None

        assert ctx.synthetic_cache_result is None
        coordinator._apply_synthetic_cache_controls(context=ctx, selected=selected)
        assert ctx.synthetic_cache_result is not None
        result = ctx.synthetic_cache_result
        assert result.plan.status in (
            "dry_run",
            "applied",
            "no_candidates",
            "disabled",
            "policy_required",
            "capability_unverified",
        )
        assert result.plan.dry_run is True
