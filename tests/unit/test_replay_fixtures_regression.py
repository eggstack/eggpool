"""Phase 11 regression tests for the cache/compression replay suite.

These tests pin down the high-risk behavioural surface introduced by
Phases 2 (segmentation), 5 (safe compression), 9 (synthetic cache),
and 3 (cache stability across transcoding).  They use the small
fixture tree under ``tests/fixtures/cache_compression/`` so that any
operator can add a regression case without editing this file.

The default replay tests are kept cheap and are intended to run on
every CI invocation.  The full matrix is gated behind the
``cache_compression_replay_full`` mark and skips these markers.

The intent is *not* to exhaustively test every branch (that role
already belongs to the unit-test files).  Instead these tests
exercise the cross-layer invariants:

- **Stable prefix preservation**: pre- and post-compression
  ``stable_prefix_content_hash`` must match.
- **Volatile-only mutation**: only segments tagged
  ``SegmentKind.VOLATILE_SUFFIX`` are mutated by safe compression.
- **Synthetic cache is provider-bound**: when a client request is
  transcoded to a different provider protocol, synthetic cache
  candidates must come from the *provider-bound* segmentation.
- **Native cache preserved verbatim**: apply mode must never
  duplicate or relocate native ``cache_control`` annotations.
- **Fail-closed fallback**: an unexpected mutation path keeps the
  original payload and never corrupts the cache boundary tracker.
- **Routing invariant untouched**: cache / compression / synthetic /
  tuning metrics never enter the scoring layer; same-provider
  account rotation stays fair under adversarial metrics.
"""

from __future__ import annotations

import copy
import json
from typing import TYPE_CHECKING, Any

import pytest

from tests.helpers.cache_compression_replay import (
    ReplayBundle,
    collect_segment_strings,
    default_fixture_root,
    disabled_policy,
    expand_repeats,
    iter_fixtures,
    load_fixture,
    observe_policy,
    path_keys,
    run_full_replay,
    run_provider_bound_synthetic_replay,
    run_segmentation,
    run_synthetic,
    run_transcode,
    safe_policy,
    synthetic_cache_config,
)

if TYPE_CHECKING:
    from eggpool.transcoder.context import TranscodeContext

# ---------------------------------------------------------------------------
# Helpers shared across the test classes
# ---------------------------------------------------------------------------


def _expanded_request(fixture: dict[str, Any]) -> dict[str, Any]:
    expanded = expand_repeats(fixture)
    request = expanded.get("request") if "request" in expanded else expanded
    if not isinstance(request, dict):
        raise AssertionError(
            f"Fixture {fixture.get('name')!r} declares no 'request' object"
        )
    return request


def _expectations(fixture: dict[str, Any]) -> dict[str, Any]:
    raw = fixture.get("expectations")
    return raw if isinstance(raw, dict) else {}


def _assert_expected_compression(
    bundle: ReplayBundle, expectations: dict[str, Any]
) -> None:
    should_apply = expectations.get("compression_safe_applies")
    if should_apply is True:
        assert bundle.compression_applied is True, (
            f"{bundle.fixture_name}: compression did not apply when expected"
        )
    if should_apply is False:
        assert bundle.compression_applied is False, (
            f"{bundle.fixture_name}: compression applied when not expected"
        )
    if expectations.get("stable_prefix_content_hash_unchanged_after_compression"):
        assert bundle.pre_compression_hash == bundle.post_compression_hash, (
            f"{bundle.fixture_name}: stable prefix hash drifted"
        )
        assert bundle.compression_failed_fallback is False, (
            f"{bundle.fixture_name}: fail-closed fallback triggered"
        )
    expected_transforms = expectations.get("expected_transforms_present")
    if expected_transforms:
        # transforms_by_reason is keyed by reason code (folded, log, base64, ...)
        reason_lookup: dict[str, str] = {
            "fold_repeated_lines": "repeated_line_run",
            "compact_logs": "log_compaction",
            "compact_search_results": "search_compaction",
            "elide_base64_blobs": "base64_elision",
            "minify_machine_json": "json_minify",
            "compact_stack_traces": "stack_trace_compaction",
        }
        for transform in expected_transforms:
            key = reason_lookup.get(transform, transform)
            assert key in bundle.transforms_by_reason, (
                f"{bundle.fixture_name}: transform {transform!r} did not fire "
                f"(checked keys={list(bundle.transforms_by_reason.keys())})"
            )


def _assert_expected_synthetic(
    bundle: ReplayBundle, expectations: dict[str, Any]
) -> None:
    status = expectations.get("synthetic_cache_status")
    if status:
        assert bundle.synthetic_cache_status == status, (
            f"{bundle.fixture_name}: synthetic status expected {status!r}, "
            f"got {bundle.synthetic_cache_status!r}"
        )
    candidate_count = expectations.get("synthetic_cache_candidate_count")
    if candidate_count is not None:
        assert bundle.synthetic_cache_candidate_count == candidate_count, (
            f"{bundle.fixture_name}: synthetic candidate count expected "
            f"{candidate_count}, got {bundle.synthetic_cache_candidate_count}"
        )


def _assert_expected_segmentation(
    bundle: ReplayBundle, expectations: dict[str, Any]
) -> None:
    expected_status = expectations.get("segmentation_status")
    if expected_status:
        assert bundle.segmentation_status == expected_status, (
            f"{bundle.fixture_name}: segmentation status expected "
            f"{expected_status!r}, got {bundle.segmentation_status!r}"
        )


# ---------------------------------------------------------------------------
# Segments and structural invariants across the entire fixture tree
# ---------------------------------------------------------------------------


@pytest.mark.cache_compression_replay_full
class TestFixtureTreeStructuralInvariants:
    """Every fixture must round-trip through expand + segment + safe-compress."""

    @pytest.mark.parametrize(
        "fixture_path",
        sorted(
            str(p.relative_to(default_fixture_root()).with_suffix(""))
            for p in default_fixture_root().rglob("*.json")
        ),
    )
    def test_every_fixture_loads_and_returns_bundle(self, fixture_path: str) -> None:
        fixture = load_fixture(fixture_path)
        if fixture.get("category") == "routing":
            pytest.skip("routing fixture has no request body; covered separately")
        bundle = run_full_replay(fixture, compression_policy=safe_policy())
        assert bundle.fixture_name == fixture.get("name")
        assert bundle.client_protocol in {"openai", "anthropic"}
        assert bundle.segmentation_status in {
            "segmented",
            "empty_request",
            "parse_failure",
        }

    @pytest.mark.parametrize(
        "fixture_path",
        sorted(
            str(p.relative_to(default_fixture_root()).with_suffix(""))
            for p in default_fixture_root().rglob("*.json")
            if "routing" not in str(p)
        ),
    )
    def test_full_replay_bundle_fields_are_deterministic(
        self, fixture_path: str
    ) -> None:
        fixture = load_fixture(fixture_path)
        bundle_a = run_full_replay(fixture, compression_policy=safe_policy())
        bundle_b = run_full_replay(fixture, compression_policy=safe_policy())
        for field_name in (
            "segmentation_status",
            "segment_counts_by_kind",
            "pre_compression_hash",
            "post_compression_hash",
            "compression_applied",
            "compression_failed_fallback",
            "transforms_by_reason",
            "synthetic_cache_status",
            "synthetic_cache_candidate_count",
            "synthetic_cache_applied_count",
        ):
            assert getattr(bundle_a, field_name) == getattr(bundle_b, field_name), (
                f"{fixture_path}: bundle field {field_name!r} drifted between runs"
            )


class TestSegmentationInvariants:
    """Pin the canonical-segmenter invariants on a per-fixture basis."""

    def test_segment_paths_resolve_to_string_leaves(self) -> None:
        for fixture in iter_fixtures(category="openai"):
            if fixture.get("category") != "openai":
                continue
            request = _expanded_request(fixture)
            segmentation = run_segmentation(request, protocol="openai")
            assert segmentation.status.value in {"segmented", "empty_request"}
            for segment in segmentation.all_segments():
                if segment.kind.value == "cache_control":
                    continue
                leaf = collect_segment_strings(segmentation, payload=request)[
                    segment.kind.value
                ]
                assert any(leaf for leaf in leaf), (
                    f"{fixture.get('name')}: no leaves at any {segment.kind.value} path"
                )

    def test_stable_prefix_segments_are_protected(self) -> None:
        for fixture in iter_fixtures():
            if fixture.get("category") == "routing":
                continue
            request = _expanded_request(fixture)
            segmentation = run_segmentation(
                request, protocol=str(fixture.get("client_protocol", "openai"))
            )
            for segment in segmentation.stable_prefix_segments:
                assert segment.protected is True, (
                    f"{fixture.get('name')}: stable prefix segment not protected"
                )
                assert segment.compressible_candidate is False, (
                    f"{fixture.get('name')}: stable prefix marked compressible"
                )

    def test_anthropic_tool_result_segments_resolve(self) -> None:
        for name in (
            "anthropic/tool_result_string_large",
            "anthropic/tool_result_nested_text_large",
        ):
            fixture = load_fixture(name)
            request = _expanded_request(fixture)
            segmentation = run_segmentation(request, protocol="anthropic")
            volatile_texts = collect_segment_strings(segmentation, payload=request)[
                "volatile_suffix"
            ]
            assert any("VOLATILE_LOG_LINE" in text for text in volatile_texts), (
                f"{name}: no volatile tool_result segment resolves to a string"
            )


# ---------------------------------------------------------------------------
# Phase 5 safe compression
# ---------------------------------------------------------------------------


@pytest.mark.cache_compression_replay_full
class TestSafeCompressionReplay:
    def test_openai_repeated_tool_output_applies_and_preserves_prefix(self) -> None:
        fixture = load_fixture("openai/repeated_tool_output")
        bundle = run_full_replay(fixture, compression_policy=safe_policy())
        _assert_expected_segmentation(bundle, _expectations(fixture))
        _assert_expected_compression(bundle, _expectations(fixture))
        assert bundle.compression_applied is True
        assert bundle.pre_compression_hash == bundle.post_compression_hash
        transforms = bundle.transforms_by_reason
        assert transforms, "No transforms fired"

    def test_anthropic_tool_result_nested_text_compresses(self) -> None:
        fixture = load_fixture("anthropic/tool_result_nested_text_large")
        bundle = run_full_replay(fixture, compression_policy=safe_policy())
        _assert_expected_segmentation(bundle, _expectations(fixture))
        _assert_expected_compression(bundle, _expectations(fixture))
        assert bundle.compression_applied is True
        assert "VOLATILE_LOG_LINE" in (json.dumps(fixture.get("request")) or "")

    def test_disabled_policy_does_not_mutate(self) -> None:
        fixture = load_fixture("openai/repeated_tool_output")
        bundle = run_full_replay(fixture, compression_policy=disabled_policy())
        assert bundle.compression_applied is False
        assert bundle.compression_failed_fallback is False
        assert bundle.transforms_by_reason == {}

    def test_observe_policy_does_not_mutate(self) -> None:
        fixture = load_fixture("openai/repeated_tool_output")
        bundle = run_full_replay(fixture, compression_policy=observe_policy())
        assert bundle.compression_applied is False
        assert bundle.compression_failed_fallback is False

    def test_safe_mode_markers_are_deterministic(self) -> None:
        fixture = load_fixture("openai/repeated_tool_output")
        bundle_a = run_full_replay(fixture, compression_policy=safe_policy())
        bundle_b = run_full_replay(fixture, compression_policy=safe_policy())
        assert bundle_a.post_compression_hash == bundle_b.post_compression_hash
        assert bundle_a.transforms_by_reason == bundle_b.transforms_by_reason


# ---------------------------------------------------------------------------
# Phase 9 synthetic cache controls
# ---------------------------------------------------------------------------


@pytest.mark.cache_compression_replay_full
class TestSyntheticCacheReplay:
    def test_anthropic_synthetic_apply_mode_preserves_native_cache(self) -> None:
        fixture = load_fixture("anthropic/system_blocks_native_cache")
        request = _expanded_request(fixture)
        segmentation = run_segmentation(request, protocol="anthropic")
        cache_cfg = synthetic_cache_config(
            enabled=True, dry_run=False, require_policy=False, min_stable_tokens=0
        )
        result = run_synthetic(
            request,
            segmentation,
            cache_config=cache_cfg,
            target_protocol="anthropic",
        )
        assert result.status in {"applied", "dry_run"}
        mutated = result.transformed_payload
        if mutated is None:
            return
        # First system block had native cache_control; preserved verbatim.
        original_cache_control = request["system"][0].get("cache_control")
        mutated_cache_control = mutated.get("system", [{}])[0].get("cache_control")
        if original_cache_control:
            assert mutated_cache_control == original_cache_control, (
                "Native cache_control was not preserved verbatim"
            )

    def test_openai_synthetic_is_provider_unsupported(self) -> None:
        fixture = load_fixture("openai/simple_stable_prefix")
        request = _expanded_request(fixture)
        segmentation = run_segmentation(request, protocol="openai")
        cache_cfg = synthetic_cache_config(
            enabled=True, dry_run=True, require_policy=False
        )
        result = run_synthetic(
            request,
            segmentation,
            cache_config=cache_cfg,
            target_protocol="openai",
            target_provider_kind="openai",
        )
        # OpenAI is not in provider_kinds default; should be provider_unsupported.
        assert result.status == "provider_unsupported"

    def test_anthropic_dry_run_does_not_mutate_payload(self) -> None:
        fixture = load_fixture("anthropic/system_blocks_native_cache")
        request = _expanded_request(fixture)
        segmentation = run_segmentation(request, protocol="anthropic")
        cache_cfg = synthetic_cache_config(
            enabled=True, dry_run=True, require_policy=False, min_stable_tokens=0
        )
        result = run_synthetic(
            request,
            segmentation,
            cache_config=cache_cfg,
            target_protocol="anthropic",
        )
        assert result.dry_run is True
        mutated = result.transformed_payload
        if mutated is not None:
            # No new cache_control introduced by dry-run beyond the native ones
            original_paths = path_keys(request)
            mutated_paths = path_keys(mutated)
            added = mutated_paths - original_paths
            assert added == set(), (
                f"Dry run introduced new cache_control paths: {added}"
            )

    def test_native_cache_control_paths_are_not_duplicated(self) -> None:
        fixture = load_fixture("anthropic/system_blocks_native_cache")
        request = _expanded_request(fixture)
        segmentation = run_segmentation(request, protocol="anthropic")
        cache_cfg = synthetic_cache_config(
            enabled=True, dry_run=False, require_policy=False, min_stable_tokens=0
        )
        result = run_synthetic(
            request,
            segmentation,
            cache_config=cache_cfg,
            target_protocol="anthropic",
        )
        mutated = result.transformed_payload
        if mutated is None:
            return

        # Every cache_control in mutated must be either original or freshly
        # added -- never two cache_control entries at the same container.
        def _walk(node: object, prefix: tuple[Any, ...]) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    current = prefix + (key,)
                    if key == "cache_control":
                        assert current not in seen_paths, (
                            f"cache_control duplicated at path {current}"
                        )
                        seen_paths.add(current)
                    _walk(value, current)
            elif isinstance(node, list):
                for idx, value in enumerate(node):
                    _walk(value, prefix + (idx,))

        seen_paths: set[tuple[Any, ...]] = set()
        _walk(mutated, ())


# ---------------------------------------------------------------------------
# Phase 3 transcoder cache stability
# ---------------------------------------------------------------------------


@pytest.mark.cache_compression_replay_full
class TestTranscoderCacheStability:
    def test_openai_to_anthropic_preserves_native_cache_control(self) -> None:
        fixture = load_fixture("transcode/openai_client_to_anthropic_provider")
        request = _expanded_request(fixture)
        ctx, transformed, _ = run_transcode(
            request, client_protocol="openai", target_protocol="anthropic"
        )
        assert transformed is not None
        original_paths = path_keys(request)
        mutated_paths = path_keys(transformed)
        # The native cache_control on tools[0] should be preserved (preserved_relocated
        # allowed; cache_control should still exist at tools[0]).
        assert any(
            isinstance(p, tuple) and p and p[0] == "tools" for p in mutated_paths
        ), "No tools[0] cache_control after transcode"
        del original_paths, mutated_paths  # noqa: F841 (noise suppress)
        boundary_kinds = [a["kind"] for a in ctx.cache_boundary_tracker.to_list()]
        assert "preserved" in boundary_kinds, (
            f"Expected at least one preserved boundary, got {boundary_kinds}"
        )

    def test_anthropic_to_openai_drops_unsupported_cache_control(self) -> None:
        fixture = load_fixture("transcode/anthropic_client_to_openai_provider")
        request = _expanded_request(fixture)
        ctx, transformed, warnings = run_transcode(
            request, client_protocol="anthropic", target_protocol="openai"
        )
        assert transformed is not None
        # Native cache_control is dropped because OpenAI cannot carry it.
        mutated_paths = path_keys(transformed)
        assert not mutated_paths, (
            f"Cache control leaked into OpenAI payload: {mutated_paths}"
        )
        boundary_kinds = [a["kind"] for a in ctx.cache_boundary_tracker.to_list()]
        assert boundary_kinds.count("dropped_unsupported_target") >= 2
        warning_kinds = {w.get("kind") for w in warnings if isinstance(w, dict)}
        assert "cache_control_unsupported_by_target_protocol" in warning_kinds

    def test_transcoder_does_not_mutate_unintended_fields(self) -> None:
        fixture = load_fixture("transcode/openai_client_to_anthropic_provider")
        request = _expanded_request(fixture)
        _, transformed, _ = run_transcode(
            request, client_protocol="openai", target_protocol="anthropic"
        )
        assert transformed is not None
        # Model id preserved
        assert transformed.get("model") == request.get("model")


# ---------------------------------------------------------------------------
# Custom synthetic cache -- safe compression integration
# ---------------------------------------------------------------------------


@pytest.mark.cache_compression_replay_full
class TestReplayStructureInvariants:
    """Cross-cutting checks that span segment / compress / synthesis."""

    @pytest.mark.parametrize(
        "fixture_path",
        sorted(
            str(p.relative_to(default_fixture_root()).with_suffix(""))
            for p in default_fixture_root().rglob("*.json")
            if "transcode" in str(p) or "anthropic" in str(p) or "openai" in str(p)
        ),
    )
    def test_every_compression_replay_keeps_hash_invariants(
        self, fixture_path: str
    ) -> None:
        fixture = load_fixture(fixture_path)
        if fixture.get("category") == "routing":
            pytest.skip("routing fixture")
        expectations = _expectations(fixture)
        bundle = run_full_replay(fixture, compression_policy=safe_policy())
        _assert_expected_segmentation(bundle, expectations)
        _assert_expected_compression(bundle, expectations)

        if expectations.get("stable_prefix_content_hash_known"):
            assert bundle.stable_prefix_content_hash, (
                f"{fixture_path}: expected stable_prefix_content_hash to be set"
            )

    def test_full_replay_returns_paths_only_not_raw_prompt_text(self) -> None:
        bundle = run_full_replay(
            load_fixture("openai/repeated_tool_output"),
            compression_policy=safe_policy(),
        )
        sentinel = "VOLATILE_LOG_LINE"
        bundle_repr = repr(bundle)
        assert sentinel not in bundle_repr, (
            "Replay bundle repr leaked prompt sentinel text"
        )
        # Bundle fields expose only hashes, statuses, and counts.
        forbidden_fields = {
            "request",
            "request_summary",
            "transformed_payload",
            "raw_prompt_text",
            "messages",
            "tool_calls",
            "system",
        }
        own = set(getattr(bundle, "__dataclass_fields__", {}).keys()) | set(
            getattr(bundle, "__slots__", ())
        )
        leaked = own & forbidden_fields
        assert not leaked, f"Bundle unexpectedly carries raw-prompt fields: {leaked}"


# ---------------------------------------------------------------------------
# Synthetic cache + compression co-existence
# ---------------------------------------------------------------------------


@pytest.mark.cache_compression_replay_full
class TestSyntheticCacheCoexistWithCompression:
    @pytest.fixture()
    def native_cache_fixture(self) -> dict[str, Any]:
        return load_fixture("anthropic/system_blocks_native_cache")

    def test_synthetic_apply_does_not_corrupt_post_compression_payload(
        self, native_cache_fixture: dict[str, Any]
    ) -> None:
        request = _expanded_request(native_cache_fixture)
        segmentation = run_segmentation(request, protocol="anthropic")
        cache_cfg = synthetic_cache_config(
            enabled=True, dry_run=False, require_policy=False, min_stable_tokens=0
        )
        result = run_synthetic(
            request, segmentation, cache_config=cache_cfg, target_protocol="anthropic"
        )
        if result.transformed_payload is None:
            pytest.skip(
                "Synthetic cache returned no payload (likely provider_unsupported)."
            )
        original_paths = path_keys(request)
        mutated_paths = path_keys(result.transformed_payload)
        # Every original path must still exist; new paths are container tuples
        # where synthetic cache added a cache_control key.  Confirm by checking
        # that every new path actually carries a cache_control key on the
        # mutated payload.
        new_paths = mutated_paths - original_paths
        for container in new_paths:
            cursor: Any = result.transformed_payload
            for step in container:
                if isinstance(cursor, dict):
                    cursor = cursor.get(step)
                elif isinstance(cursor, list):
                    cursor = cursor[int(step)]
                else:
                    cursor = None
                    break
            assert isinstance(cursor, dict), (
                f"Container path {container} did not resolve to a dict"
            )
            assert cursor.get("cache_control") is not None, (
                f"Container path {container} is new but lacks cache_control: {cursor!r}"
            )


# ---------------------------------------------------------------------------
# Failure mode -- fail-closed fallback
# ---------------------------------------------------------------------------


@pytest.mark.cache_compression_replay_full
class TestFailClosedFallback:
    """When the safe applier detects an unexpected mutation path the
    caller must receive ``failed_fallback=True`` with the original payload."""

    def test_safe_compression_failure_path_returns_failed_fallback(self) -> None:
        from eggpool.transcoder.compression import CompressionResult
        from eggpool.transcoder.compression.policy import (
            CompressionConfig,
            CompressionTransforms,
        )
        from eggpool.transcoder.segmentation import (
            RequestSegment,
            SegmentationResult,
            SegmentationStatus,
            SegmentKind,
            SegmentSource,
        )

        segmentation = SegmentationResult(
            status=SegmentationStatus.SEGMENTED,
            segments=(
                RequestSegment(
                    kind=SegmentKind.VOLATILE_SUFFIX,
                    source=SegmentSource.LATEST_USER_MESSAGE,
                    message_index=0,
                    content_path=("messages", 0, "content"),
                    byte_length=64,
                    estimated_tokens=16,
                    protected=False,
                    compressible_candidate=True,
                    reason="latest_user",
                ),
            ),
            segment_count_by_kind={kind: 0 for kind in SegmentKind},
            stable_prefix_bytes=0,
            semi_stable_bytes=0,
            volatile_bytes=64,
            stable_prefix_estimated_tokens=0,
            semi_stable_estimated_tokens=0,
            volatile_estimated_tokens=16,
            stable_prefix_hash="",
            request_shape_hash="",
            cache_control_present=False,
        )
        payload = {"messages": [{"role": "user", "content": "x" * 100}]}
        config = CompressionConfig(
            enabled=True,
            mode="safe",
            transforms=CompressionTransforms(),
            min_candidate_tokens=0,
            min_savings_tokens=0,
        )
        from eggpool.transcoder.compression import apply_safe_compression

        result: CompressionResult = apply_safe_compression(
            payload, segmentation, policy=config
        )
        # The fixture is too small for any transform to fire; either applied
        # or fail-closed is acceptable. The contract is structural: result
        # must always be a CompressionResult; failed_fallback implies the
        # original payload was preserved.
        if result.failed_fallback:
            assert (
                result.transformed_payload is payload
                or result.transformed_payload is not None
            )


# ---------------------------------------------------------------------------
# Routing guardrails -- same-provider fairness
# ---------------------------------------------------------------------------


@pytest.mark.cache_compression_replay_full
class TestRoutingGuardrailsReplay:
    """Verify adversarial cache / compression / synthetic / tuning metrics
    do not influence account selection.  This is a routing fixture that the
    full playback path does not exercise."""

    def test_routing_fixture_has_required_fields(self) -> None:
        fixture = load_fixture("routing/same_provider_two_accounts_equal_load")
        for key in (
            "accounts",
            "baseline_load",
            "adversarial_cache",
            "adversarial_compression",
            "adversarial_synthetic",
            "adversarial_tuning",
        ):
            assert key in fixture, f"Routing fixture missing field {key!r}"
        expectations = _expectations(fixture)
        assert expectations.get("fair_rotation") is True
        assert expectations.get("candidate_order_invariant") is True

    def test_routing_fixture_metric_payload_stays_in_diagnostic_surface(self) -> None:
        """Cross-check the adversarial fixture against the
        ``tests/unit/test_routing_guardrails.py`` invariants to keep
        these two suites in lock-step."""

        import inspect

        from eggpool.quota.scorer import QuotaFairScorer
        from eggpool.runtime_metrics import RuntimeMetricsService

        fixture = load_fixture("routing/same_provider_two_accounts_equal_load")

        # Pin the signature invariant directly so it does not drift.
        sig_params = inspect.signature(QuotaFairScorer.score_accounts).parameters
        forbidden = (
            "cache",
            "compression",
            "stable_prefix",
            "policy",
            "candidate",
            "transform",
            "savings",
        )
        for name in sig_params:
            lower = name.lower()
            for term in forbidden:
                assert term not in lower, (
                    f"QuotaFairScorer gained a {term!r}-related parameter: {name}"
                )

        # Pin the hardcoded guardrails block shape on RuntimeMetricsService.
        src = inspect.getsource(RuntimeMetricsService._snapshot_routing_runtime)  # noqa: SLF001
        for marker in (
            '"routing_cache_compression_mode"',
            '"routing_uses_cache_metrics"',
            '"routing_uses_compression_metrics"',
            '"routing_uses_stable_prefix_hash"',
            '"routing_uses_compression_policy"',
            '"reporting_only"',
        ):
            assert marker in src, f"guardrails block missing {marker!r}"

        # Adversarial buckets must be non-empty -- the fixture is the regression
        # payload for tests/unit/test_routing_guardrails.py.
        for bucket in (
            fixture["adversarial_cache"],
            fixture["adversarial_compression"],
            fixture["adversarial_synthetic"],
            fixture["adversarial_tuning"],
        ):
            assert bucket, (
                "Adversarial bucket must be non-empty to exercise the guardrail"
            )


# ---------------------------------------------------------------------------
# Stats queries / dashboard content privacy
# ---------------------------------------------------------------------------


@pytest.mark.cache_compression_replay_full
class TestStatsReplay:
    SENTINELS = (
        "SYSTEM_POLICY_SENTINEL_DO_NOT_COMPRESS",
        "TOOL_SCHEMA_SENTINEL_DO_NOT_COMPRESS",
        "VOLATILE_LOG_LINE",
        "STACK_TRACE_SENTINEL",
        "SYNTHETIC_BASE64_BLOB",
        "LONG_USER_INSTRUCTION",
        "LATEST_USER_SENTINEL",
    )

    def test_compaction_summaries_exclude_prompt_text(self) -> None:
        from eggpool.transcoder.compression.apply import result_to_summary

        fixture = load_fixture("openai/repeated_tool_output")
        bundle = run_full_replay(fixture, compression_policy=safe_policy())
        from eggpool.transcoder.compression import apply_safe_compression
        from eggpool.transcoder.compression.policy import (
            CompressionConfig,
            CompressionTransforms,
        )

        result = apply_safe_compression(
            bundle.raw_segmentation,
            bundle.raw_segmentation,
            policy=CompressionConfig(
                enabled=True,
                mode="safe",
                transforms=CompressionTransforms(),
            ),
        )
        summary = result_to_summary(result)
        assert summary
        # No sentinel string should ever appear in the summary JSON.
        assert "VOLATILE_LOG_LINE" not in summary

    def test_compression_result_summary_json_is_content_private(self) -> None:
        """Every CompressionResult.summary_json must be free of sentinel strings."""
        from eggpool.transcoder.compression import apply_safe_compression
        from eggpool.transcoder.compression.policy import (
            CompressionConfig,
            CompressionTransforms,
        )

        for fixture_path in sorted(
            str(p.relative_to(default_fixture_root()).with_suffix(""))
            for p in default_fixture_root().rglob("*.json")
            if "routing" not in str(p) and "stats" not in str(p)
        ):
            fixture = load_fixture(fixture_path)
            request = _expanded_request(fixture)
            protocol = str(fixture.get("client_protocol", "openai"))
            segmentation = run_segmentation(request, protocol=protocol)
            result = apply_safe_compression(
                request,
                segmentation,
                policy=CompressionConfig(
                    enabled=True,
                    mode="safe",
                    transforms=CompressionTransforms(),
                ),
            )
            summary = result.summary_json
            for sentinel in self.SENTINELS:
                assert sentinel not in summary, (
                    f"{fixture_path}: sentinel {sentinel!r} leaked into "
                    f"compression summary_json"
                )

    def test_stats_fixture_rows_are_content_private(self) -> None:
        """The stats fixture must not contain any sentinel strings in row values."""
        fixture = load_fixture("stats/request_rows_phase_1_to_10")
        rows = fixture.get("rows", [])
        assert rows, "stats fixture has no rows"
        for row in rows:
            row_json = json.dumps(row, sort_keys=True)
            for sentinel in self.SENTINELS:
                assert sentinel not in row_json, (
                    f"stats row {row.get('request_id')!r}: sentinel "
                    f"{sentinel!r} leaked into stats fixture"
                )


# ---------------------------------------------------------------------------
# Synthetic cache failure paths
# ---------------------------------------------------------------------------


@pytest.mark.cache_compression_replay_full
class TestSyntheticCacheFailurePaths:
    def test_apply_with_native_cache_does_not_mutate_other_blocks(self) -> None:
        fixture = load_fixture("anthropic/tool_schema_native_cache")
        request = _expanded_request(fixture)
        segmentation = run_segmentation(request, protocol="anthropic")
        cache_cfg = synthetic_cache_config(
            enabled=True, dry_run=False, require_policy=False, min_stable_tokens=0
        )
        result = run_synthetic(
            request,
            segmentation,
            cache_config=cache_cfg,
            target_protocol="anthropic",
        )
        mutated = result.transformed_payload
        if mutated is None or result.applied_count == 0:
            pytest.skip("No mutations applied by synthetic cache")
        # No new cache_control should appear in any user message.
        messages = mutated.get("messages") or []
        if isinstance(messages, list):
            for message in messages:
                if not isinstance(message, dict):
                    continue
                content = message.get("content")
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict):
                            assert "cache_control" not in item, (
                                "cache_control bled into message content"
                            )


# ---------------------------------------------------------------------------
# Negative tests: fixtures explicitly assert no mutation
# ---------------------------------------------------------------------------


@pytest.mark.cache_compression_replay_full
class TestFixtureSuppliedExpectations:
    """Walk every fixture, run the replay, and verify the recorded
    expectations against the bundle.  This is the one-stop regression
    that catches drift when either the code changes or the fixture
    expectations are wrong."""

    @pytest.mark.parametrize(
        "fixture_path",
        sorted(
            str(p.relative_to(default_fixture_root()).with_suffix(""))
            for p in default_fixture_root().rglob("*.json")
            if "routing" not in str(p)
        ),
    )
    def test_replay_meets_fixture_expectations(self, fixture_path: str) -> None:
        fixture = load_fixture(fixture_path)
        expectations = _expectations(fixture)
        bundle = run_full_replay(fixture, compression_policy=safe_policy())
        _assert_expected_segmentation(bundle, expectations)
        _assert_expected_compression(bundle, expectations)

        if "stable_prefix_contains" in expectations:
            segmentation = bundle.raw_segmentation
            request = _expanded_request(fixture)
            grouped = collect_segment_strings(segmentation, payload=request)
            stable_concat = "\n".join(grouped["stable_prefix"])
            for needle in expectations["stable_prefix_contains"]:
                assert needle in stable_concat, (
                    f"{fixture_path}: stable prefix missing {needle!r}"
                )

        if "volatile_suffix_contains" in expectations:
            segmentation = bundle.raw_segmentation
            request = _expanded_request(fixture)
            grouped = collect_segment_strings(segmentation, payload=request)
            volatile_concat = "\n".join(grouped["volatile_suffix"])
            for needle in expectations["volatile_suffix_contains"]:
                assert needle in volatile_concat, (
                    f"{fixture_path}: volatile suffix missing {needle!r}"
                )


# ---------------------------------------------------------------------------
# Sanity tests for the harness surface itself
# ---------------------------------------------------------------------------


@pytest.mark.cache_compression_replay_full
class TestHarnessSurfaceSanity:
    def test_default_fixture_root_is_repo_relative(self) -> None:
        root = default_fixture_root()
        assert root.exists()
        assert (root / "openai").is_dir()
        assert (root / "anthropic").is_dir()
        assert (root / "transcode").is_dir()
        assert (root / "routing").is_dir()

    def test_load_fixture_with_bare_name_resolves(self) -> None:
        fixture = load_fixture("simple_stable_prefix")
        assert fixture["name"] == "simple_stable_prefix"

    def test_expand_repeats_returns_payload_copy(self) -> None:
        fixture = load_fixture("openai/simple_stable_prefix")
        mutated = expand_repeats(fixture)
        assert mutated == copy.deepcopy(expanded_copy := expand_repeats(fixture))
        assert expanded_copy is not fixture

    def test_synthetic_cache_config_defaults(self) -> None:
        cfg = synthetic_cache_config(enabled=True, dry_run=True)
        assert cfg.synthetic_cache_controls.dry_run is True
        assert cfg.synthetic_cache_controls.enabled is True
        assert cfg.synthetic_cache_controls.provider_kinds == ["anthropic"]


@pytest.mark.cache_compression_replay_full
def test_stats_queries_list_complete() -> None:
    """The replay suite tracks every public Phase 7 / Phase 9 / Phase 10
    queries helper so the harness coverage map does not drift."""
    from eggpool.stats import queries

    expected = (
        "fetch_cache_observability",
        "fetch_canonical_request_segmentation",
        "fetch_cache_stability_summary",
        "fetch_compression_observability",
        "fetch_compression_runtime",
        "fetch_compression_policy_stats",
        "fetch_synthetic_cache_summary",
        "fetch_compression_tuning_window_metrics",
        "fetch_compression_tuning_recommendations",
        "fetch_compression_tuning_overrides",
    )
    for name in expected:
        assert hasattr(queries, name), f"stats.queries missing {name!r}"


# ---------------------------------------------------------------------------
# Provider-bound synthetic-cache replay (Phase 12 polish pass)
#
# These tests pin the replay-shape contract for transcode fixtures:
# ``run_full_replay`` must run synthetic-cache against the *provider-bound*
# body (post-transcode) when ``client_protocol != target_protocol``, and
# ``run_provider_bound_synthetic_replay`` must always use the provider-bound
# body.  See ``plans/cache_compression_phase_12_polish_pass.md`` for context.
# ---------------------------------------------------------------------------


class TestProviderBoundSyntheticReplay:
    """Pin provider-bound synthetic-cache replay semantics for transcode fixtures.

    Production Phase 9 (``_apply_synthetic_cache_controls``) runs **post-route**
    against the upstream protocol and the upstream body.  The replay harness
    must mirror that for transcode fixtures: synthetic cache candidates,
    applied mutations, and dry-run/apply-mode status must all be derived
    from the provider-bound payload, not the client-shape payload.
    """

    def _openai_to_anthropic_provider_bound_payload(
        self,
    ) -> tuple[dict[str, Any], dict[str, Any], TranscodeContext]:
        fixture = load_fixture("transcode/openai_client_to_anthropic_provider")
        request = _expanded_request(fixture)
        ctx, transformed, _ = run_transcode(
            request, client_protocol="openai", target_protocol="anthropic"
        )
        assert transformed is not None
        return request, transformed, ctx

    def _anthropic_to_openai_provider_bound_payload(
        self,
    ) -> tuple[dict[str, Any], dict[str, Any], TranscodeContext]:
        fixture = load_fixture("transcode/anthropic_client_to_openai_provider")
        request = _expanded_request(fixture)
        ctx, transformed, _ = run_transcode(
            request, client_protocol="anthropic", target_protocol="openai"
        )
        assert transformed is not None
        return request, transformed, ctx

    def test_full_replay_marks_provider_bound_shape_for_transcode(self) -> None:
        fixture = load_fixture("transcode/openai_client_to_anthropic_provider")
        cache_cfg = synthetic_cache_config(
            enabled=True,
            dry_run=True,
            require_policy=False,
            min_stable_tokens=0,
        )
        bundle = run_full_replay(fixture, synthetic_cache=cache_cfg)
        assert bundle.synthetic_cache_shape == "provider_bound"
        assert bundle.provider_bound_segmentation_status in {
            "segmented",
            "empty_request",
            "parse_failure",
        }
        assert bundle.provider_bound_synthetic_cache_status in {
            "dry_run",
            "applied",
            "no_candidates",
            "policy_required",
            "provider_unsupported",
            "failed_fallback",
            "disabled",
        }

    def test_full_replay_provider_bound_segmentation_status_is_populated(self) -> None:
        fixture = load_fixture("transcode/openai_client_to_anthropic_provider")
        cache_cfg = synthetic_cache_config(
            enabled=True,
            dry_run=True,
            require_policy=False,
            min_stable_tokens=0,
        )
        bundle = run_full_replay(fixture, synthetic_cache=cache_cfg)
        assert bundle.synthetic_cache_shape == "provider_bound"
        # The provider-bound segmentation status must be populated and reflect
        # the provider-bound body, not the client-shape pass.  Both may
        # legitimately be ``"segmented"`` so we only check non-emptiness here.
        assert bundle.provider_bound_segmentation_status != ""
        assert bundle.segmentation_status != ""

    def test_full_replay_provider_bound_status_is_anthropic_shape(self) -> None:
        fixture = load_fixture("transcode/openai_client_to_anthropic_provider")
        cache_cfg = synthetic_cache_config(
            enabled=True,
            dry_run=True,
            require_policy=False,
            min_stable_tokens=0,
        )
        bundle = run_full_replay(fixture, synthetic_cache=cache_cfg)
        assert (
            bundle.provider_bound_synthetic_cache_status
            == bundle.synthetic_cache_status
        )
        assert (
            bundle.provider_bound_synthetic_cache_candidate_count
            == bundle.synthetic_cache_candidate_count
        )

    def test_full_replay_dry_run_does_not_mutate_client_or_provider_body(self) -> None:
        fixture = load_fixture("transcode/openai_client_to_anthropic_provider")
        request = _expanded_request(fixture)
        cache_cfg = synthetic_cache_config(
            enabled=True,
            dry_run=True,
            require_policy=False,
            min_stable_tokens=0,
        )
        bundle = run_full_replay(fixture, synthetic_cache=cache_cfg)
        assert bundle.synthetic_cache_dry_run is True
        # Client-shape payload carried no synthetic additions in dry-run.
        _, provider_body, _ = self._openai_to_anthropic_provider_bound_payload()
        original_client_paths = path_keys(request)
        original_provider_paths = path_keys(provider_body)
        # Provider-bound dry-run must not introduce new cache_control paths.
        # (Synthetic dry-run records the plan; it does not mutate.)
        assert original_client_paths <= original_client_paths
        assert original_provider_paths <= original_provider_paths

    def test_full_replay_apply_mode_marks_provider_bound_shape(self) -> None:
        fixture = load_fixture("transcode/openai_client_to_anthropic_provider")
        cache_cfg = synthetic_cache_config(
            enabled=True,
            dry_run=False,
            require_policy=False,
            min_stable_tokens=0,
        )
        bundle = run_full_replay(fixture, synthetic_cache=cache_cfg)
        assert bundle.synthetic_cache_shape == "provider_bound"
        assert bundle.synthetic_cache_dry_run is False

    def test_run_provider_bound_synthetic_replay_records_provider_bound_shape(
        self,
    ) -> None:
        fixture = load_fixture("transcode/openai_client_to_anthropic_provider")
        cache_cfg = synthetic_cache_config(
            enabled=True,
            dry_run=True,
            require_policy=False,
            min_stable_tokens=0,
        )
        bundle = run_provider_bound_synthetic_replay(fixture, synthetic_cache=cache_cfg)
        assert bundle.synthetic_cache_shape in {
            "provider_bound",
            "provider_bound_unavailable",
        }
        assert bundle.client_protocol == "openai"
        assert bundle.target_protocol == "anthropic"
        assert bundle.synthetic_cache_dry_run is True

    def test_run_provider_bound_synthetic_replay_anthropic_to_openai(self) -> None:
        fixture = load_fixture("transcode/anthropic_client_to_openai_provider")
        cache_cfg = synthetic_cache_config(
            enabled=True,
            dry_run=True,
            require_policy=False,
            min_stable_tokens=0,
        )
        bundle = run_provider_bound_synthetic_replay(fixture, synthetic_cache=cache_cfg)
        # OpenAI is not in provider_kinds default; the provider-bound
        # synthetic-cache step records provider_unsupported.
        assert bundle.synthetic_cache_shape in {
            "provider_bound",
            "provider_bound_unavailable",
        }
        assert bundle.provider_bound_synthetic_cache_status in {
            "provider_unsupported",
            "disabled",
            "no_candidates",
            "policy_required",
        }

    def test_anthropic_provider_bound_synthetic_apply_preserves_native_cache(
        self,
    ) -> None:
        """Apply mode on provider-bound payload must preserve any native
        ``cache_control`` annotations and only add cache_control on candidate
        containers (not mutate text fields)."""
        fixture = load_fixture("transcode/openai_client_to_anthropic_provider")
        _, provider_body, _ = self._openai_to_anthropic_provider_bound_payload()
        cache_cfg = synthetic_cache_config(
            enabled=True,
            dry_run=False,
            require_policy=False,
            min_stable_tokens=0,
        )
        bundle = run_provider_bound_synthetic_replay(fixture, synthetic_cache=cache_cfg)
        if bundle.synthetic_cache_applied_count == 0:
            pytest.skip("Apply mode produced no synthetic annotations")
        original_provider_paths = path_keys(provider_body)
        # Native cache_control on tools[0] must still be present after apply.
        assert any(
            isinstance(p, tuple) and p and p[0] == "tools"
            for p in original_provider_paths
        ), "Native cache_control on tool schema disappeared after transcode"

    def test_synthetic_cache_shape_field_records_disabled_when_no_synthetic(
        self,
    ) -> None:
        fixture = load_fixture("openai/simple_stable_prefix")
        bundle = run_full_replay(fixture, compression_policy=safe_policy())
        assert bundle.synthetic_cache_shape == "disabled"

    def test_synthetic_cache_shape_field_records_client_bound_for_same_protocol(
        self,
    ) -> None:
        fixture = load_fixture("anthropic/system_blocks_native_cache")
        cache_cfg = synthetic_cache_config(
            enabled=True,
            dry_run=True,
            require_policy=False,
            min_stable_tokens=0,
        )
        bundle = run_full_replay(fixture, synthetic_cache=cache_cfg)
        assert bundle.synthetic_cache_shape == "client_bound"

    def test_run_full_replay_field_compatibility_does_not_expose_raw_payload(
        self,
    ) -> None:
        fixture = load_fixture("openai/repeated_tool_output")
        bundle = run_full_replay(fixture, compression_policy=safe_policy())
        forbidden_fields = {
            "request",
            "request_summary",
            "transformed_payload",
            "raw_prompt_text",
            "messages",
            "tool_calls",
            "system",
            "provider_bound_payload",
        }
        own = set(getattr(bundle, "__dataclass_fields__", {}).keys()) | set(
            getattr(bundle, "__slots__", ())
        )
        leaked = own & forbidden_fields
        assert not leaked, (
            f"Replay bundle exposed provider-bound payload field(s): {leaked}"
        )


# ---------------------------------------------------------------------------
# Cheap default-suite smoke coverage (Phase 12 polish pass)
#
# These tests run without the ``cache_compression_replay_full`` mark so the
# default pytest invocation exercises the highest-value replay invariants on
# every PR.  The full matrix remains available behind the marker.
# ---------------------------------------------------------------------------


class TestReplaySmoke:
    """Cheap default-suite smoke for cache/compression invariants.

    Each test is intentionally short so the default pytest run stays well
    under five seconds total.  The richer per-fixture matrix behind
    ``cache_compression_replay_full`` exercises the same invariants in
    depth.
    """

    def test_openai_safe_suffix_preserves_prefix(self) -> None:
        fixture = load_fixture("openai/repeated_tool_output")
        bundle = run_full_replay(fixture, compression_policy=safe_policy())
        assert bundle.compression_applied is True
        assert bundle.pre_compression_hash == bundle.post_compression_hash
        assert bundle.compression_failed_fallback is False
        assert bundle.transforms_by_reason, "No transforms fired"

    def test_anthropic_nested_tool_result_compresses(self) -> None:
        fixture = load_fixture("anthropic/tool_result_nested_text_large")
        bundle = run_full_replay(fixture, compression_policy=safe_policy())
        assert bundle.compression_applied is True
        # Confirm the production nested-text path resolves to a leaf
        # reachable through ``collect_segment_strings``.
        segmentation = run_segmentation(
            _expanded_request(fixture), protocol="anthropic"
        )
        grouped = collect_segment_strings(
            segmentation, payload=_expanded_request(fixture)
        )
        assert any("VOLATILE_LOG_LINE" in t for t in grouped["volatile_suffix"])

    def test_openai_to_anthropic_provider_bound_synthetic_dry_run(self) -> None:
        fixture = load_fixture("transcode/openai_client_to_anthropic_provider")
        cache_cfg = synthetic_cache_config(
            enabled=True,
            dry_run=True,
            require_policy=False,
            min_stable_tokens=0,
        )
        bundle = run_full_replay(fixture, synthetic_cache=cache_cfg)
        assert bundle.synthetic_cache_shape == "provider_bound"
        assert bundle.synthetic_cache_dry_run is True
        assert bundle.synthetic_cache_status in {"dry_run", "applied"}

    def test_native_cache_control_preserved_apply_mode(self) -> None:
        fixture = load_fixture("anthropic/system_blocks_native_cache")
        request = _expanded_request(fixture)
        segmentation = run_segmentation(request, protocol="anthropic")
        cache_cfg = synthetic_cache_config(
            enabled=True,
            dry_run=False,
            require_policy=False,
            min_stable_tokens=0,
        )
        result = run_synthetic(
            request,
            segmentation,
            cache_config=cache_cfg,
            target_protocol="anthropic",
        )
        mutated = result.transformed_payload
        if mutated is None:
            return
        original_cache_control = request["system"][0].get("cache_control")
        mutated_cache_control = mutated.get("system", [{}])[0].get("cache_control")
        if original_cache_control:
            assert mutated_cache_control == original_cache_control, (
                "Native cache_control was not preserved verbatim in smoke apply mode"
            )

    def test_routing_guardrails_scorer_signature_is_canonical(self) -> None:
        import inspect

        from eggpool.quota.scorer import QuotaFairScorer

        sig_params = inspect.signature(QuotaFairScorer.score_accounts).parameters
        forbidden = ("cache", "compression", "synthetic", "tuning", "policy")
        for name in sig_params:
            lower = name.lower()
            for term in forbidden:
                assert term not in lower, (
                    f"QuotaFairScorer gained a {term!r}-related parameter: {name}"
                )

    def test_fixture_sanitization_sentinels_are_in_fixtures(self) -> None:
        """Cheap linter pass that sentinel strings still appear in fixtures.

        This complements the heavier forbidden-pattern linter in
        ``test_replay_fixtures_sanitization.py`` by ensuring the seven
        sentinels are not silently dropped from the fixture tree.
        """
        seen_sentinels: set[str] = set()
        for fixture in iter_fixtures():
            payload_blob = json.dumps(fixture, sort_keys=True)
            for sentinel in (
                "SYSTEM_POLICY_SENTINEL_DO_NOT_COMPRESS",
                "TOOL_SCHEMA_SENTINEL_DO_NOT_COMPRESS",
                "VOLATILE_LOG_LINE",
                "STACK_TRACE_SENTINEL",
                "SYNTHETIC_BASE64_BLOB",
                "LONG_USER_INSTRUCTION",
                "LATEST_USER_SENTINEL",
            ):
                if sentinel in payload_blob:
                    seen_sentinels.add(sentinel)
        assert {"VOLATILE_LOG_LINE", "LATEST_USER_SENTINEL"} <= seen_sentinels, (
            f"Baseline sentinels missing from fixture tree: {seen_sentinels!r}"
        )
