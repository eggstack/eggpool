"""Realistic replay fixtures for safe-mode compression (Phase 5.13).

Plan task 13 calls for "realistic fixtures" of the kinds of payloads
that flood prompts in real coding-agent workflows:

- Large pytest failure output
- Rust ``cargo test`` / ``cargo check`` output
- Python traceback loops (recursion-style frames)
- ripgrep output over many files
- pip install noise
- base64 image / blob accidentally pasted into tool output
- large JSON API response

Each fixture asserts:

- the expected transform fires
- the transformed output preserves diagnostic material
  (errors, file paths, line numbers, exit codes)
- stable prefix hash is unchanged

The fixtures are intentionally hand-written so that every line is
deliberate and the assertions reflect real-world expectations rather
than mechanical properties of generated data.  Long lines in fixture
payloads are kept verbatim to preserve realism; ruff line-length is
relaxed for this file via the per-file ``noqa`` below.

Per-fixture transform gating
----------------------------

The applier iterates ALL enabled transforms per segment.  A payload
that looks like both a log AND a search-result will be acted on by
whichever transform fires first (see ``apply._apply_safe_compression_impl``
for the dispatch order).  Where a fixture is intended to exercise a
specific transform, the policy disables competing transforms — this
mirrors the pattern in ``test_compression_apply.py`` and reflects how
operators would tune ``[compression.transforms]`` in production.
"""

# ruff: noqa: E501  # realistic fixture lines are intentionally verbatim

from __future__ import annotations

import base64
import json

from eggpool.transcoder.compression import (
    CompressionConfig,
    apply_safe_compression,
)
from eggpool.transcoder.compression.policy import CompressionTransforms
from eggpool.transcoder.segmentation import (
    RequestSegment,
    SegmentationResult,
    SegmentationStatus,
    SegmentKind,
    SegmentSource,
)

# ---------------------------------------------------------------------------
# Fixture helpers — single source of truth so each scenario lives in one
# place and assertions are easy to read.
# ---------------------------------------------------------------------------


def _segmentation_for(segments: list[RequestSegment]) -> SegmentationResult:
    """Build a :class:`SegmentationResult` from a list of segments."""
    counts: dict[SegmentKind, int] = {k: 0 for k in SegmentKind}
    for s in segments:
        counts[s.kind] += 1
    return SegmentationResult(
        status=SegmentationStatus.SEGMENTED,
        segments=tuple(segments),
        segment_count_by_kind=counts,
        stable_prefix_bytes=0,
        semi_stable_bytes=0,
        volatile_bytes=sum(s.byte_length for s in segments),
        stable_prefix_estimated_tokens=None,
        semi_stable_estimated_tokens=None,
        volatile_estimated_tokens=(
            sum(s.estimated_tokens or 0 for s in segments) or None
        ),
        stable_prefix_hash="stable_prefix_hash",
        request_shape_hash="request_shape_hash",
        cache_control_present=False,
    )


def _suffix_seg(
    text: str,
    *,
    source: SegmentSource = SegmentSource.TOOL_RESULT,
    msg_index: int = 1,
) -> RequestSegment:
    """Build a volatile_suffix segment matching ``messages[<msg>].content``."""
    return RequestSegment(
        kind=SegmentKind.VOLATILE_SUFFIX,
        source=source,
        message_index=msg_index,
        content_path=("messages", msg_index, "content"),
        byte_length=len(text),
        estimated_tokens=max(len(text) // 4, 1),
        protected=False,
        compressible_candidate=True,
        reason="tool_result",
    )


_TRANSFORM_OVERRIDE_KEYS = frozenset(
    {
        "fold_repeated_lines",
        "compact_logs",
        "compact_search_results",
        "elide_base64_blobs",
        "minify_machine_json",
        "compact_stack_traces",
    }
)


def _safe_policy(**overrides: object) -> CompressionConfig:
    """Safe-mode config with permissive thresholds for fixture testing.

    Defaults to enabling only the transforms appropriate for the
    realistic-fixture scenarios below; specific fixtures override to
    exercise other transforms.  Transform flags are recognised by name
    and routed through ``CompressionTransforms``; everything else is
    passed as a top-level field on ``CompressionConfig``.
    """
    transform_defaults: dict[str, bool] = {
        "fold_repeated_lines": True,
        "compact_logs": True,
        "compact_search_results": False,
        "elide_base64_blobs": True,
        "minify_machine_json": False,
        "compact_stack_traces": True,
    }
    top_overrides: dict[str, object] = dict(
        enabled=True,
        mode="safe",
        placement="suffix_only",
        respect_cache_boundaries=True,
        compress_static_prefix=False,
        min_candidate_tokens=0,
        min_savings_tokens=0,
        max_compression_latency_ms=100.0,
    )
    for key, value in overrides.items():
        if key in _TRANSFORM_OVERRIDE_KEYS:
            transform_defaults[key] = value  # type: ignore[assignment]
        else:
            top_overrides[key] = value
    return CompressionConfig(
        **top_overrides,  # type: ignore[arg-type]
        transforms=CompressionTransforms(**transform_defaults),
    )


def _two_message_payload(system_text: str, tool_text: str) -> dict[str, object]:
    """Build a payload with a stable_prefix system message and a
    volatile_suffix tool result message — the canonical Phase 5 shape."""
    return {
        "model": "claude-sonnet-4",
        "messages": [
            {"role": "system", "content": system_text},
            {"role": "tool", "content": tool_text},
        ],
    }


# ---------------------------------------------------------------------------
# Fixture 1: Large pytest failure output
# ---------------------------------------------------------------------------

PYTEST_FAIL_OUTPUT = """\
============================= test session starts ==============================
platform linux -- Python 3.11.6, pytest-7.4.2, pluggy-1.3.0
rootdir: /home/user/eggpool
configfile: pyproject.toml
plugins: asyncio-0.23.2, respx-0.21.0, cov-4.1.0
collected 412 items

tests/unit/test_compression_apply.py::test_applies_fold_repeated_lines PASSED
tests/unit/test_compression_apply.py::test_fold_repeated_lines_marker_appended PASSED
tests/unit/test_compression_apply.py::test_fold_repeated_lines_digest_is_64_hex PASSED
tests/unit/test_compression_apply.py::test_stable_prefix_preserved PASSED
tests/unit/test_compression_apply.py::test_protected_stable_prefix_never_mutated PASSED
tests/unit/test_compression_apply.py::test_applies_minify_machine_json PASSED
tests/unit/test_compression_apply.py::test_applies_elide_base64_blobs PASSED
tests/unit/test_compression_apply.py::test_applies_compact_logs PASSED
tests/unit/test_compression_apply.py::test_applies_compact_stack_traces PASSED
tests/unit/test_compression_apply.py::test_applies_compact_search_results PASSED
tests/unit/test_compression_apply.py::test_below_min_candidate_tokens_suppresses PASSED
tests/unit/test_compression_apply.py::test_below_min_savings_tokens_suppresses PASSED
tests/unit/test_compression_apply.py::test_disabled_transform_not_applied PASSED
tests/unit/test_compression_apply.py::test_latency_budget_exceeded_records_warning PASSED
tests/unit/test_compression_apply.py::test_payload_not_mutated_in_place PASSED
tests/unit/test_compression_apply.py::test_failed_fallback_on_prefix_hash_mismatch PASSED
tests/unit/test_compression_apply.py::test_summary_json_contains_all_fields PASSED
tests/unit/test_compression_apply.py::test_multiple_transforms_in_one_segment PASSED
tests/unit/test_compression_apply.py::test_multiple_volatile_segments PASSED
tests/unit/test_compression_apply.py::test_empty_segmentation_returns_noop PASSED
tests/unit/test_compression_apply.py::test_semi_stable_segment_suppressed_by_placement PASSED
tests/unit/test_compression_markers.py::test_marker_round_trip PASSED
tests/unit/test_compression_markers.py::test_marker_without_optional_fields PASSED
tests/unit/test_compression_markers.py::test_marker_with_unicode_segment_id PASSED
tests/unit/test_compression_markers.py::test_marker_digest_is_64_hex PASSED
tests/unit/test_compression_markers.py::test_marker_token_count_preserved PASSED
tests/unit/test_compression_markers.py::test_is_marker_line_recognises_marker PASSED
tests/unit/test_compression_markers.py::test_is_marker_line_rejects_arbitrary_text PASSED
tests/unit/test_compression_markers.py::test_parse_marker_handles_whitespace PASSED
tests/unit/test_compression_markers.py::test_build_marker_is_deterministic PASSED
tests/unit/test_compression_markers.py::test_marker_segment_id_uniqueness PASSED
tests/unit/test_compression_markers.py::test_marker_token_count_zero PASSED
tests/unit/test_compression_markers.py::test_marker_long_segment_id PASSED
tests/unit/test_compression_markers.py::test_marker_negative_line_count_rejected PASSED
tests/unit/test_compression_policy.py::test_safe_mode_accepted PASSED
tests/unit/test_compression_policy.py::test_observe_mode_accepted PASSED
tests/unit/test_compression_policy.py::test_unknown_mode_rejected PASSED
tests/unit/test_compression_policy.py::test_static_prefix_override_required_in_safe_mode PASSED

FAILED tests/unit/test_compression_apply.py::test_failed_fallback_on_prefix_hash_mismatch - AssertionError: assert result.transform_count == 0
FAILED tests/unit/test_compression_apply.py::test_multiple_transforms_in_one_segment - RuntimeError: synthetic transform crash
============= 2 failed, 410 passed, 24 warnings in 47.32s ==============

FAILED tests/unit/test_compression_apply.py::test_failed_fallback_on_prefix_hash_mismatch - AssertionError: assert result.transform_count == 0
_____________ test_failed_fallback_on_prefix_hash_mismatch _____________

    def test_failed_fallback_on_prefix_hash_mismatch() -> None:
        \"\"\"When prefix hash changes unexpectedly, original payload is returned.\"\"\"
        repeated = "ERR\\n" * 10
        text = repeated + "OK\\n"
        payload = {"messages": [{"role": "tool", "content": text}]}
        seg = _vol_seg(byte_length=len(text), estimated_tokens=len(text) // 4)
        segmentation = _segmentation([seg])
        # Force a prefix hash mismatch by patching the segment list
>       result = apply_safe_compression(
            payload, segmentation, policy=_enabled_safe_policy()
        )
E       AssertionError: assert result.transform_count == 0
E        +  where result = CompressionResult(applied=False, ..., transform_count=1, ...)

FAILED tests/unit/test_compression_apply.py::test_multiple_transforms_in_one_segment - RuntimeError: synthetic transform crash
_____________ test_multiple_transforms_in_one_segment _____________

    def test_multiple_transforms_in_one_segment() -> None:
        \"\"\"Two transforms applied to the same segment; counts aggregate.\"\"\"
        repeated = "ERR\\n" * 100
        text = repeated + "{\\"k\\": \\"v\\", \\"x\\": [1, 2, 3]}\\n"
        payload = {"messages": [{"role": "tool", "content": text}]}
        seg = _vol_seg(byte_length=len(text), estimated_tokens=len(text) // 4)
        segmentation = _segmentation([seg])
        # Force a synthetic crash in the second transform
>       result = apply_safe_compression(
            payload, segmentation, policy=_enabled_safe_policy()
        )
E       RuntimeError: synthetic transform crash

========== short test summary info ==========
FAILED tests/unit/test_compression_apply.py::test_failed_fallback_on_prefix_hash_mismatch - AssertionError
FAILED tests/unit/test_compression_apply.py::test_multiple_transforms_in_one_segment - RuntimeError
========== 2 failed, 410 passed, 24 warnings in 47.32s ==========
"""


def test_fixture_pytest_failure_output_preserves_diagnostics() -> None:
    """Large pytest failure output is compacted by ``compact_logs``.

    Verifies:
    - the head (collected items) and tail (summary) survive
    - FAILED entries and traceback heads survive
    - a deterministic compaction marker is emitted
    - stable prefix hash is unchanged
    - system prompt is byte-for-byte unchanged
    """
    system_text = "You are a senior Python engineer. Be precise."
    payload = _two_message_payload(system_text, PYTEST_FAIL_OUTPUT)
    seg = _suffix_seg(PYTEST_FAIL_OUTPUT, source=SegmentSource.COMMAND_OUTPUT)
    result = apply_safe_compression(
        payload,
        _segmentation_for([seg]),
        policy=_safe_policy(compact_logs=True),
    )

    assert result.applied is True
    assert result.stable_prefix_preserved is True
    transformed = result.transformed_payload["messages"][1]["content"]

    # Diagnostic FAILED entries survive (the diagnostics-pattern matcher
    # in ``compact_logs`` keeps ERROR/FATAL/EXCEPTION/PANIC/FAILED lines).
    assert (
        "FAILED tests/unit/test_compression_apply.py::test_failed_fallback_on_prefix_hash_mismatch"
        in transformed
    )
    assert (
        "FAILED tests/unit/test_compression_apply.py::test_multiple_transforms_in_one_segment"
        in transformed
    )
    # Final summary tail survives
    assert "2 failed, 410 passed" in transformed
    # Short test summary info block survives
    assert "short test summary info" in transformed
    # Traceback frame headers survive
    assert (
        "_____________ test_failed_fallback_on_prefix_hash_mismatch _____________"
        in (transformed)
    )
    # Compaction marker is present
    assert "[EggPool" in transformed
    # Strictly reduced
    assert len(transformed) < len(PYTEST_FAIL_OUTPUT)
    # System prompt byte-for-byte unchanged
    assert result.transformed_payload["messages"][0]["content"] == system_text


# ---------------------------------------------------------------------------
# Fixture 2: Rust cargo test output
# ---------------------------------------------------------------------------

CARGO_TEST_OUTPUT = """\
   Compiling eggpool v0.42.0 (/home/user/eggpool)
    Finished test [unoptimized + debuginfo] target(s) in 1m 23s
     Running unittests src/lib.rs (target/debug/deps/eggpool-1f4e7c2b3a5d6e8f)

running 287 tests
test result: ok. 287 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 4.82s

     Running unittests src/transcoder/compression/policy.rs (target/debug/deps/eggpool-transcoder-compression-policy-9c8e7b6a5d4f)

running 12 tests
test compression::policy::tests::test_safe_mode_default ... ok
test compression::policy::tests::test_observe_mode_default ... ok
test compression::policy::tests::test_static_prefix_override_rejected ... ok
test compression::policy::tests::test_unknown_mode_rejected ... ok
test compression::policy::tests::test_min_candidate_tokens_default ... ok
test compression::policy::tests::test_min_savings_tokens_default ... ok
test compression::policy::tests::test_max_compression_latency_default ... ok
test compression::policy::tests::test_respect_cache_boundaries_default ... ok
test compression::policy::tests::test_transform_toggle_default ... ok
test compression::policy::tests::test_header_override_default ... ok
test compression::policy::tests::test_header_cache_policy_default ... ok
test compression::policy::tests::test_allow_static_prefix_override_default ... ok

test result: ok. 12 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.08s

     Running unittests src/transcoder/compression/apply.rs (target/debug/deps/eggpool-transcoder-compression-apply-7b3c9e2a8f4d)

running 23 tests
test compression::apply::tests::test_disabled_policy_returns_noop ... ok
test compression::apply::tests::test_observe_mode_returns_noop ... ok
test compression::apply::tests::test_none_segmentation_returns_noop ... ok
test compression::apply::tests::test_applies_fold_repeated_lines ... ok
test compression::apply::tests::test_fold_repeated_lines_marker_appended ... ok
test compression::apply::tests::test_fold_repeated_lines_digest_is_64_hex ... ok
test compression::apply::tests::test_stable_prefix_preserved ... ok
test compression::apply::tests::test_protected_stable_prefix_never_mutated ... ok
test compression::apply::tests::test_applies_minify_machine_json ... ok
test compression::apply::tests::test_applies_elide_base64_blobs ... ok
test compression::apply::tests::test_applies_compact_logs ... ok
test compression::apply::tests::test_applies_compact_stack_traces ... ok
test compression::apply::tests::test_applies_compact_search_results ... ok
test compression::apply::tests::test_below_min_candidate_tokens_suppresses ... ok
test compression::apply::tests::test_below_min_savings_tokens_suppresses ... ok
test compression::apply::tests::test_disabled_transform_not_applied ... ok
test compression::apply::tests::test_latency_budget_exceeded_records_warning ... ok
test compression::apply::tests::test_payload_not_mutated_in_place ... ok
test compression::apply::tests::test_failed_fallback_on_prefix_hash_mismatch ... ok
test compression::apply::tests::test_summary_json_contains_all_fields ... ok
test compression::apply::tests::test_multiple_transforms_in_one_segment ... ok
test compression::apply::tests::test_multiple_volatile_segments ... ok
test compression::apply::tests::test_empty_segmentation_returns_noop ... ok

test result: ok. 23 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.14s

     Running unittests src/transcoder/compression/markers.rs (target/debug/deps/eggpool-transcoder-compression-markers-4e8c2a9f6b3d)

running 13 tests
test compression::markers::tests::test_marker_round_trip ... ok
test compression::markers::tests::test_marker_without_optional_fields ... ok
test compression::markers::tests::test_marker_with_unicode_segment_id ... ok
test compression::markers::tests::test_marker_digest_is_64_hex ... ok
test compression::markers::tests::test_marker_token_count_preserved ... ok
test compression::markers::tests::test_is_marker_line_recognises_marker ... ok
test compression::markers::tests::test_is_marker_line_rejects_arbitrary_text ... ok
test compression::markers::tests::test_parse_marker_handles_whitespace ... ok
test compression::markers::tests::test_build_marker_is_deterministic ... ok
test compression::markers::tests::test_marker_segment_id_uniqueness ... ok
test compression::markers::tests::test_marker_token_count_zero ... ok
test compression::markers::tests::test_marker_long_segment_id ... ok
test compression::markers::tests::test_marker_negative_line_count_rejected ... ok

test result: ok. 13 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.06s

     Running unittests src/db/repositories.rs (target/debug/deps/eggpool-db-repositories-2a9f4e7c3b8d)

running 9 tests
test db::repositories::tests::test_finalize_if_pending_basic ... ok
test db::repositories::tests::test_finalize_if_pending_with_phase5 ... ok
test db::repositories::tests::test_finalize_if_pending_phase5_zero_count ... ok
test db::repositories::tests::test_finalize_if_pending_phase5_mixed ... ok
test db::repositories::tests::test_finalize_if_pending_idempotent ... ok
test db::repositories::tests::test_finalize_if_pending_missing_request ... ok
test db::repositories::tests::test_finalize_if_pending_phase5_unset ... ok
test db::repositories::tests::test_finalize_if_pending_phase5_invalid_json ... ok
test db::repositories::tests::test_finalize_if_pending_phase5_summary_frozen ... ok

test result: ok. 9 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.04s

     Running unittests src/stats/queries.rs (target/debug/deps/eggpool-stats-queries-5f7b2e9c4a8d)

running 5 tests
test stats::queries::tests::test_fetch_compression_observability_basic ... ok
test stats::queries::tests::test_fetch_compression_observability_phase5 ... ok
test stats::queries::tests::test_fetch_compression_observability_phase5_savings ... ok
test stats::queries::tests::test_fetch_compression_observability_phase5_top_reason_codes ... ok
test stats::queries::tests::test_fetch_compression_observability_phase5_provider_status ... ok

test result: ok. 5 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.03s
"""


def test_fixture_cargo_test_output_preserves_head_and_tail_summary() -> None:
    """Long ``cargo test`` output is compacted by ``compact_logs``.

    ``compact_logs`` keeps 8 head + 8 tail lines and emits a marker for
    the dropped middle.  For cargo output the operationally critical
    artefacts are the first ``Compiling`` head and the last
    ``test result: ok.`` tail — both must survive.

    Verifies:
    - the ``Compiling eggpool`` head line survives
    - the last ``test result: ok.`` tail line survives
    - a compaction marker is emitted for the dropped middle
    - stable prefix hash is unchanged
    """
    system_text = "You are a senior Rust engineer."
    payload = _two_message_payload(system_text, CARGO_TEST_OUTPUT)
    seg = _suffix_seg(CARGO_TEST_OUTPUT, source=SegmentSource.COMMAND_OUTPUT)
    result = apply_safe_compression(
        payload,
        _segmentation_for([seg]),
        policy=_safe_policy(compact_logs=True),
    )

    assert result.applied is True
    assert result.stable_prefix_preserved is True
    transformed = result.transformed_payload["messages"][1]["content"]

    # Head "Compiling eggpool" survives
    assert "Compiling eggpool" in transformed
    # Tail "Finished test ... target(s)" head survives
    assert "Finished test [unoptimized + debuginfo] target(s)" in transformed
    # Last test-result summary (the tail line) survives
    assert "5 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out" in transformed
    # Marker is present
    assert "[EggPool" in transformed
    # Compacted
    assert len(transformed) < len(CARGO_TEST_OUTPUT)
    # System prompt byte-for-byte unchanged
    assert result.transformed_payload["messages"][0]["content"] == system_text


# ---------------------------------------------------------------------------
# Fixture 3: Rust cargo check output
# ---------------------------------------------------------------------------

CARGO_CHECK_OUTPUT = """\
   Compiling proc-macro2 v1.0.66
   Compiling unicode-ident v1.0.11
   Compiling syn v2.0.27
   Compiling eggpool v0.42.0 (/home/user/eggpool)
   Compiling tokio v1.34.0
   Compiling hyper v0.14.27
   Compiling reqwest v0.11.20
   Compiling serde v1.0.183
   Compiling serde_json v1.0.105
   Compiling chrono v0.4.26
   Compiling ahash v0.8.3
   Compiling hashbrown v0.14.0
   Compiling rusqlite v0.29.0
   Compiling sqlx-macros v0.7.2
   Compiling thiserror v1.0.48
   Compiling anyhow v1.0.74
   Compiling tracing v0.1.37
   Compiling tracing-subscriber v0.3.17
   Compiling eggpool-transcoder v0.42.0 (/home/user/eggpool)
   Compiling eggpool-router v0.42.0 (/home/user/eggpool)
   Compiling eggpool-cli v0.42.0 (/home/user/eggpool)
warning: unused variable: `response_headers`
   --> src/api/proxy_request.rs:412:9
    |
412 |     let response_headers = extract_response_headers(&ctx);
    |         ^^^^^^^^^^^^^^^^ help: if this is intentional, prefix it with an underscore: `_response_headers`
    |
    = note: `#[warn(unused_variables)]` implied by `#[warn(unused)]`

warning: unused variable: `response_headers`
   --> src/api/proxy_request.rs:413:9
    |
413 |     let response_headers = normalize_headers(&ctx);
    |         ^^^^^^^^^^^^^^^^ help: if this is intentional, prefix it with an underscore: `_response_headers`
    |
    = note: `#[warn(unused_variables)]` implied by `#[warn(unused)]`

error[E0308]: mismatched types
   --> src/transcoder/compression/apply.rs:284:13
    |
284 |     run_start: usize = 0
    |             ^^^^^ expected `usize`, found `u32`
    |
    = note: expected type `usize`
               found type `u32`

error[E0277]: the trait bound `String: From<CompressionResult>` is not satisfied
   --> src/transcoder/compression/apply.rs:301:7
    |
301 |     let summary: String = result.into();
    |                  -----         ^^^^^^^^ the trait `From<CompressionResult>` is not satisfied
    |
    = help: consider implementing `From<CompressionResult>` for `String`
    = note: required for `String` to impl `From<CompressionResult>`

error: aborting due to 2 previous errors; 2 warnings emitted
"""


def test_fixture_cargo_check_output_preserves_errors_and_files() -> None:
    """``cargo check`` error output keeps diagnostic lines.

    ``compact_logs`` keeps 8 head + 8 tail lines and preserves
    ERROR/PANIC/FATAL/FAILED diagnostic lines from the middle.  For
    ``cargo check`` the operationally critical artefacts are the
    error codes themselves (``error[E0308]`` etc.) and the
    ``error: aborting due to N previous errors`` summary tail.

    Note: the ``--> file:line:col`` source-location lines that sit
    between an error header and its source-snippet block do not match
    the diagnostic-pattern matcher, so they may be dropped from the
    middle.  This is a known limitation of the head/middle/tail
    compaction strategy — the error codes themselves (which carry the
    code context the operator needs) always survive.

    Verifies:
    - the head ``Compiling proc-macro2`` line survives
    - the tail ``error: aborting due to 2 previous errors`` survives
    - error[E0308] / error[E0277] lines survive (they match the
      ERROR-pattern matcher)
    - the source-snippet context lines survive (via the tail block)
    - stable prefix hash is unchanged
    """
    system_text = "You are a senior Rust engineer."
    payload = _two_message_payload(system_text, CARGO_CHECK_OUTPUT)
    seg = _suffix_seg(CARGO_CHECK_OUTPUT, source=SegmentSource.COMMAND_OUTPUT)
    result = apply_safe_compression(
        payload,
        _segmentation_for([seg]),
        policy=_safe_policy(compact_logs=True),
    )

    assert result.applied is True
    assert result.stable_prefix_preserved is True
    transformed = result.transformed_payload["messages"][1]["content"]

    # Head "Compiling" line survives
    assert "Compiling proc-macro2" in transformed
    # Tail error summary survives
    assert "error: aborting due to 2 previous errors" in transformed
    assert "2 warnings emitted" in transformed
    # Error codes survive (ERROR pattern)
    assert "error[E0308]" in transformed
    assert "error[E0277]" in transformed
    # The source-snippet context for the second error is in the tail
    # block — the ``= help:`` / ``= note:`` lines survive.
    assert "= help: consider implementing `From<CompressionResult>` for `String`" in (
        transformed
    )
    # Marker is present
    assert "[EggPool" in transformed
    # Compacted
    assert len(transformed) < len(CARGO_CHECK_OUTPUT)
    # System prompt byte-for-byte unchanged
    assert result.transformed_payload["messages"][0]["content"] == system_text


# ---------------------------------------------------------------------------
# Fixture 4: Python traceback loops (recursion-style frames)
# ---------------------------------------------------------------------------

PYTHON_TRACEBACK_LOOP = """\
Traceback (most recent call last):
  File "/app/eggpool/transcoder/compression/apply.py", line 487, in _apply_safe_compression_impl
    transforms_by_reason[reason_code] = (
  File "/app/eggpool/transcoder/compression/apply.py", line 487, in _apply_safe_compression_impl
    transforms_by_reason[reason_code] = (
  File "/app/eggpool/transcoder/compression/apply.py", line 487, in _apply_safe_compression_impl
    transforms_by_reason[reason_code] = (
  File "/app/eggpool/transcoder/compression/apply.py", line 487, in _apply_safe_compression_impl
    transforms_by_reason[reason_code] = (
  File "/app/eggpool/transcoder/compression/apply.py", line 487, in _apply_safe_compression_impl
    transforms_by_reason[reason_code] = (
  File "/app/eggpool/transcoder/compression/apply.py", line 487, in _apply_safe_compression_impl
    transforms_by_reason[reason_code] = (
  File "/app/eggpool/transcoder/compression/apply.py", line 487, in _apply_safe_compression_impl
    transforms_by_reason[reason_code] = (
  File "/app/eggpool/transcoder/compression/apply.py", line 487, in _apply_safe_compression_impl
    transforms_by_reason[reason_code] = (
  File "/app/eggpool/utils/recursion.py", line 12, in recurse
    return recurse(n - 1)
  File "/app/eggpool/utils/recursion.py", line 12, in recurse
    return recurse(n - 1)
  File "/app/eggpool/utils/recursion.py", line 12, in recurse
    return recurse(n - 1)
  File "/app/eggpool/utils/recursion.py", line 12, in recurse
    return recurse(n - 1)
  File "/app/eggpool/utils/recursion.py", line 12, in recurse
    return recurse(n - 1)
  File "/app/eggpool/utils/recursion.py", line 12, in recurse
    return recurse(n - 1)
  File "/app/eggpool/utils/recursion.py", line 12, in recurse
    return recurse(n - 1)
  File "/app/eggpool/utils/recursion.py", line 12, in recurse
    return recurse(n - 1)
  File "/app/eggpool/utils/recursion.py", line 8, in recurse
    raise RecursionError("base case")
RecursionError: base case
"""


def test_fixture_python_traceback_loop_preserves_final_exception() -> None:
    """Recursion-style traceback with repeated frames is compacted.

    The traceback contains two distinct frame signatures (apply.py:487
    and recursion.py:12) repeated many times.  ``compact_stack_traces``
    drops repeats of identical frames while keeping the first
    occurrence of each.

    Verifies:
    - the stack-compaction transform fires
    - the final ``RecursionError: base case`` line survives
    - the first occurrence of each unique frame survives
    - a stack-compaction marker is emitted
    - stable prefix hash is unchanged
    """
    system_text = "You are a senior Python engineer."
    payload = _two_message_payload(system_text, PYTHON_TRACEBACK_LOOP)
    seg = _suffix_seg(PYTHON_TRACEBACK_LOOP, source=SegmentSource.TOOL_RESULT)
    result = apply_safe_compression(
        payload,
        _segmentation_for([seg]),
        # Disable compact_logs so it does not preempt stack_traces.
        policy=_safe_policy(
            compact_logs=False,
            compact_stack_traces=True,
        ),
    )

    assert result.applied is True
    assert result.stable_prefix_preserved is True
    transformed = result.transformed_payload["messages"][1]["content"]

    # Stack compaction transform fires
    assert "stack_trace_compaction" in result.transforms_by_reason
    # Final exception survives (always — see _transform_compact_stack_traces)
    assert "RecursionError: base case" in transformed
    # The final frame survives with its file path
    assert "/app/eggpool/utils/recursion.py" in transformed
    assert "line 8" in transformed
    # First occurrence of each unique frame survives
    assert "/app/eggpool/transcoder/compression/apply.py" in transformed
    assert "/app/eggpool/utils/recursion.py" in transformed
    # Stack-compaction marker is present
    assert "[EggPool" in transformed
    # Length is reduced
    assert len(transformed) < len(PYTHON_TRACEBACK_LOOP)
    # System prompt byte-for-byte unchanged
    assert result.transformed_payload["messages"][0]["content"] == system_text


# ---------------------------------------------------------------------------
# Fixture 5: ripgrep output over many files
# ---------------------------------------------------------------------------

RIPGREP_OUTPUT = (
    "\n".join(
        [
            f"src/eggpool/api/proxy_request.py:{100 + i}:def handler_{i}(self, ctx):"
            for i in range(40)
        ]
        + [
            f"src/eggpool/api/proxy_request.py:{100 + i}:    return self.do_work_{i}()"
            for i in range(40)
        ]
        + ["src/eggpool/db/repositories.py:200:def find_by_id(self, request_id):"]
        + ["src/eggpool/db/repositories.py:201:    return self._repo.get(request_id)"]
        + ["src/eggpool/db/repositories.py:202:    # end"]
        + ["src/eggpool/request/coordinator.py:1500:async def finalize(self, ctx):"]
        + ["src/eggpool/request/coordinator.py:1501:    await self._finalizer.run(ctx)"]
        + [
            "diff --git a/src/eggpool/transcoder/compression/apply.py b/src/eggpool/transcoder/compression/apply.py",
            "@@ -480,3 +480,7 @@",
            "+            if dropped_count == 1:",
            "+                marker = (",
            '+                    f"[EggPool stack compacted: dropped {drop_count} repeated frames]"',
            "+                )",
            "+                new_lines.append(marker)",
            "+                marker_added = True",
        ]
    )
    + "\n"
)


def test_fixture_ripgrep_output_compacted_with_marker() -> None:
    """ripgrep-style output is compacted by ``compact_search_results``.

    ``compact_search_results`` keeps lines that look like search hits
    (``diff``, ``@@``, ``---``, ``+++``, ``Binary``, ``./path``,
    ``/path``) and drops redundant middle lines.

    Verifies:
    - the search-compaction transform fires
    - file:line:pattern lines survive
    - the diff hunk header survives
    - a search-compaction marker is emitted
    - stable prefix hash is unchanged
    """
    system_text = "You are a senior search engineer."
    payload = _two_message_payload(system_text, RIPGREP_OUTPUT)
    seg = _suffix_seg(RIPGREP_OUTPUT, source=SegmentSource.SEARCH_RESULT)
    result = apply_safe_compression(
        payload,
        _segmentation_for([seg]),
        # Disable compact_logs so it doesn't preempt search_results.
        policy=_safe_policy(
            compact_logs=False,
            compact_search_results=True,
        ),
    )

    assert result.applied is True
    assert result.stable_prefix_preserved is True
    transformed = result.transformed_payload["messages"][1]["content"]

    # Search-result transform fires
    assert "search_compaction" in result.transforms_by_reason
    # Diff hunk header survives
    assert "diff --git a/src/eggpool/transcoder/compression/apply.py" in transformed
    # Some file:line:content match lines survive
    assert "src/eggpool/db/repositories.py:200:" in transformed
    # Compaction marker is present
    assert "[EggPool" in transformed
    assert len(transformed) < len(RIPGREP_OUTPUT)
    # System prompt byte-for-byte unchanged
    assert result.transformed_payload["messages"][0]["content"] == system_text


# ---------------------------------------------------------------------------
# Fixture 6: pip install noise
# ---------------------------------------------------------------------------

PIP_INSTALL_OUTPUT = """\
Collecting fastapi==0.103.2
  Downloading fastapi-0.103.2-py3-none-any.whl (56 kB)
     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 56.5/56.5 kB 1.2 MB/s eta 0:00:00
Collecting uvicorn[standard]==0.23.2
  Downloading uvicorn-0.23.2-py3-none-any.whl (59 kB)
     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 59.3/59.3 kB 2.1 MB/s eta 0:00:00
Collecting pydantic==2.3.0
  Downloading pydantic-2.3.2-py3-none-any.whl (374 kB)
     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 374.4/374.4 kB 4.5 MB/s eta 0:00:00
Collecting pydantic-core==2.4.0
  Downloading pydantic_core-2.4.0-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl (1.7 MB)
     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 1.7/1.7 MB 18.3 MB/s eta 0:00:00
Collecting typing-extensions==4.7.1
  Downloading typing_extensions-4.7.1-py3-none-any.whl (33 kB)
     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 33.0/33.0 kB 5.1 MB/s eta 0:00:00
Collecting anyio==3.7.1
  Downloading anyio-3.7.1-py3-none-any.whl (82 kB)
     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 82.4/82.4 kB 8.7 MB/s eta 0:00:00
Collecting idna==3.4
  Downloading idna-3.4-py3-none-any.whl (66 kB)
     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 66.0/66.0 kB 11.2 MB/s eta 0:00:00
Collecting sniffio==1.3.0
  Downloading sniffio-1.3.0-py3-none-any.whl (10 kB)
     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 10.2/10.2 kB 4.4 MB/s eta 0:00:00
Collecting h11==0.14.0
  Downloading h11-0.14.0-py3-none-any.whl (58 kB)
     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 58.2/58.2 kB 6.8 MB/s eta 0:00:00
Collecting httpcore==0.17.3
  Downloading httpcore-0.17.3-py3-none-any.whl (77 kB)
     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 77.6/77.6 kB 9.4 MB/s eta 0:00:00
Collecting httpx==0.24.1
  Downloading httpx-0.24.1-py3-none-any.whl (75 kB)
     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 75.3/75.3 kB 7.7 MB/s eta 0:00:00
Collecting httptools==0.6.1
  Downloading httptools-0.6.1-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl (138 kB)
     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 138.0/138.0 kB 12.1 MB/s eta 0:00:00
Collecting pyyaml==6.0.1
  Downloading PyYAML-6.0.1-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl (745 kB)
     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 745.0/745.0 kB 15.6 MB/s eta 0:00:00
Collecting uvloop==0.17.0
  Downloading uvloop-0.17.0-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl (1.5 MB)
     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 1.5/1.5 MB 19.0 MB/s eta 0:00:00
Collecting watchfiles==0.20.0
  Downloading watchfiles-0.20.0-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl (1.2 MB)
     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 1.2/1.2 MB 22.1 MB/s eta 0:00:00
Collecting websockets==11.0.3
  Downloading websockets-11.0.3-cp311-cp311-manylinux_2_17_x86_64.manylinux2014_x86_64.whl (152 kB)
     ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 152.1/152.1 kB 14.0 MB/s eta 0:00:00
Installing collected packages: typing-extensions, idna, sniffio, h11, httpcore, anyio, httpx, httptools, pyyaml, uvloop, watchfiles, websockets, pydantic-core, pydantic, click, fastapi, uvicorn
Successfully installed anyio-3.7.1 click-8.1.7 fastapi-0.103.2 h11-0.14.0 httpcore-0.17.3 httptools-0.6.1 httpx-0.24.1 idna-3.4 pydantic-2.3.0 pydantic-core-2.4.0 pyyaml-6.0.1 sniffio-1.3.0 typing-extensions-4.7.1 uvloop-0.17.0 uvicorn-0.23.2 watchfiles-0.20.0 websockets-11.0.3
WARNING: There was an error checking the latest version of pip.
"""


def test_fixture_pip_install_output_compacted() -> None:
    """pip install noise is compacted by ``compact_logs``.

    The head (``Collecting fastapi==0.103.2``) and tail
    (``Successfully installed ...`` and ``WARNING``) must survive.  The
    middle is the noise that should be collapsed into a marker.

    Verifies:
    - the head Collecting line survives
    - the final ``Successfully installed`` summary survives
    - the WARNING tail survives
    - the progress-bar noise in the middle is collapsed
    - stable prefix hash is unchanged
    """
    system_text = "You are a senior Python engineer."
    payload = _two_message_payload(system_text, PIP_INSTALL_OUTPUT)
    seg = _suffix_seg(PIP_INSTALL_OUTPUT, source=SegmentSource.COMMAND_OUTPUT)
    result = apply_safe_compression(
        payload,
        _segmentation_for([seg]),
        policy=_safe_policy(compact_logs=True),
    )

    assert result.applied is True
    assert result.stable_prefix_preserved is True
    transformed = result.transformed_payload["messages"][1]["content"]

    # Head Collecting line survives
    assert "Collecting fastapi==0.103.2" in transformed
    # Final install summary survives (tail)
    assert "Successfully installed" in transformed
    assert "fastapi-0.103.2" in transformed
    # WARNING tail survives
    assert "There was an error checking the latest version of pip" in transformed
    # Marker is present
    assert "[EggPool" in transformed
    # Compacted
    assert len(transformed) < len(PIP_INSTALL_OUTPUT)
    # System prompt byte-for-byte unchanged
    assert result.transformed_payload["messages"][0]["content"] == system_text


# ---------------------------------------------------------------------------
# Fixture 7: base64 image / blob accidentally pasted into tool output
# ---------------------------------------------------------------------------


def _png_base64() -> str:
    """Return a deterministic synthetic base64 PNG-shaped payload.

    Not a real PNG — just a long random-ish ASCII string with a data URI
    prefix and a high base64 ratio.  This mirrors what an agent sees when
    a screenshot or PDF is pasted into a tool result.
    """
    raw = base64.b64encode(b"X" * 2048).decode("ascii")
    return f"data:image/png;base64,{raw}"


BLOB_OUTPUT = _png_base64()


def test_fixture_base64_blob_elided_with_digest() -> None:
    """A data-URI base64 blob in tool output is elided to a digest marker.

    Verifies:
    - the elide-base64 transform fires
    - the replacement contains a sha256= digest
    - the original blob content is no longer present (no leakage)
    - stable prefix hash is unchanged
    """
    system_text = "You are a senior frontend engineer."
    payload = _two_message_payload(system_text, BLOB_OUTPUT)
    seg = _suffix_seg(BLOB_OUTPUT, source=SegmentSource.BLOB)
    result = apply_safe_compression(
        payload,
        _segmentation_for([seg]),
        policy=_safe_policy(elide_base64_blobs=True),
    )

    assert result.applied is True
    assert result.stable_prefix_preserved is True
    transformed = result.transformed_payload["messages"][1]["content"]

    # Blob elision transform fires
    assert "base64_elision" in result.transforms_by_reason
    # Marker contains sha256
    assert "sha256=" in transformed
    # Original data URI prefix is gone
    assert "data:image/png;base64," not in transformed
    # Original blob bytes are gone
    assert _png_base64().split(",", 1)[1] not in transformed
    # Reduced
    assert len(transformed) < len(BLOB_OUTPUT)
    # System prompt byte-for-byte unchanged
    assert result.transformed_payload["messages"][0]["content"] == system_text


# ---------------------------------------------------------------------------
# Fixture 8: large JSON API response
# ---------------------------------------------------------------------------


def _large_json_response() -> str:
    """Return a large, realistic-shaped JSON API response string.

    The payload is intentionally pretty-printed so the
    ``minify_machine_json`` transform has work to do.
    """
    items = [
        {
            "id": f"usr_{i:08d}",
            "email": f"user{i}@example.com",
            "name": f"User Number {i}",
            "role": "admin" if i % 7 == 0 else "member",
            "created_at": "2026-06-15T08:42:13Z",
            "last_login_at": "2026-07-01T14:01:55Z",
            "is_active": True,
            "metadata": {
                "plan": "enterprise" if i % 3 == 0 else "starter",
                "seats": 50 if i % 3 == 0 else 5,
                "region": "us-east-1",
                "tags": ["vip", "beta"] if i % 5 == 0 else ["standard"],
            },
        }
        for i in range(120)
    ]
    return json.dumps(
        {"count": len(items), "items": items, "next_cursor": None},
        indent=2,
        sort_keys=False,
    )


LARGE_JSON_OUTPUT = _large_json_response()


def test_fixture_large_json_response_minified() -> None:
    """Large pretty-printed JSON response is whitespace-minified.

    Verifies:
    - the minify-machine-json transform fires
    - the parsed semantic content is preserved (round-trips through
      ``json.loads``)
    - object key order is preserved
    - the byte length is strictly smaller
    - stable prefix hash is unchanged
    """
    system_text = "You are a senior backend engineer."
    payload = _two_message_payload(system_text, LARGE_JSON_OUTPUT)
    seg = _suffix_seg(LARGE_JSON_OUTPUT, source=SegmentSource.TOOL_RESULT)
    result = apply_safe_compression(
        payload,
        _segmentation_for([seg]),
        policy=_safe_policy(
            compact_logs=False,
            minify_machine_json=True,
        ),
    )

    assert result.applied is True
    assert result.stable_prefix_preserved is True
    transformed = result.transformed_payload["messages"][1]["content"]

    # JSON minify transform fires
    assert "json_minify" in result.transforms_by_reason
    # Semantic preservation: transformed payload parses back to same data
    original = json.loads(LARGE_JSON_OUTPUT)
    parsed = json.loads(transformed)
    assert parsed == original
    # Key order preserved: the first item's keys are the same as input
    assert list(parsed["items"][0].keys()) == list(original["items"][0].keys())
    # Strictly smaller
    assert len(transformed) < len(LARGE_JSON_OUTPUT)
    # System prompt byte-for-byte unchanged
    assert result.transformed_payload["messages"][0]["content"] == system_text


# ---------------------------------------------------------------------------
# Cross-cutting: realistic fixtures must preserve stable prefix hash when
# only the volatile suffix mutates.
# ---------------------------------------------------------------------------


def test_fixtures_preserve_stable_prefix_hash_under_safe_mode() -> None:
    """Running every realistic fixture through safe mode preserves the
    stable prefix hash in every case.

    This is the cross-cutting safety guarantee promised by Phase 5: no
    realistic fixture should ever flip ``stable_prefix_preserved`` to
    ``False`` under default ``placement="suffix_only"``.
    """
    system_text = (
        "You are a senior software engineer. "
        "Always preserve the system prompt verbatim."
    )
    # Per-fixture policy — disable the transforms that don't apply to
    # each scenario so the assertions below remain meaningful.
    fixtures: list[tuple[str, str, SegmentSource, CompressionConfig]] = [
        (
            "pytest_failure",
            PYTEST_FAIL_OUTPUT,
            SegmentSource.COMMAND_OUTPUT,
            _safe_policy(compact_logs=True),
        ),
        (
            "cargo_test",
            CARGO_TEST_OUTPUT,
            SegmentSource.COMMAND_OUTPUT,
            _safe_policy(compact_logs=True),
        ),
        (
            "cargo_check",
            CARGO_CHECK_OUTPUT,
            SegmentSource.COMMAND_OUTPUT,
            _safe_policy(compact_logs=True),
        ),
        (
            "python_traceback",
            PYTHON_TRACEBACK_LOOP,
            SegmentSource.TOOL_RESULT,
            _safe_policy(compact_logs=False, compact_stack_traces=True),
        ),
        (
            "ripgrep",
            RIPGREP_OUTPUT,
            SegmentSource.SEARCH_RESULT,
            _safe_policy(compact_logs=False, compact_search_results=True),
        ),
        (
            "pip_install",
            PIP_INSTALL_OUTPUT,
            SegmentSource.COMMAND_OUTPUT,
            _safe_policy(compact_logs=True),
        ),
        (
            "base64_blob",
            BLOB_OUTPUT,
            SegmentSource.BLOB,
            _safe_policy(elide_base64_blobs=True),
        ),
        (
            "large_json",
            LARGE_JSON_OUTPUT,
            SegmentSource.TOOL_RESULT,
            _safe_policy(compact_logs=False, minify_machine_json=True),
        ),
    ]
    for name, text, source, policy in fixtures:
        payload = _two_message_payload(system_text, text)
        seg = _suffix_seg(text, source=source)
        result = apply_safe_compression(
            payload, _segmentation_for([seg]), policy=policy
        )
        assert result.stable_prefix_preserved is True, (
            f"{name}: stable_prefix_preserved=False — "
            "realistic fixture mutated the stable prefix"
        )
        # System message is byte-for-byte unchanged
        assert result.transformed_payload["messages"][0]["content"] == system_text, (
            f"{name}: system prompt changed under safe-mode compression"
        )


def test_fixture_disabled_safe_mode_passes_through_unchanged() -> None:
    """With safe mode disabled, every realistic fixture flows through
    unchanged.  This is the manual-verification rollback case from the
    plan: ``[compression] enabled = false`` must keep the upstream
    request byte-for-byte identical.
    """
    system_text = "You are a senior software engineer."
    fixtures = [
        PYTEST_FAIL_OUTPUT,
        CARGO_TEST_OUTPUT,
        CARGO_CHECK_OUTPUT,
        PYTHON_TRACEBACK_LOOP,
        RIPGREP_OUTPUT,
        PIP_INSTALL_OUTPUT,
        BLOB_OUTPUT,
        LARGE_JSON_OUTPUT,
    ]
    for text in fixtures:
        payload = _two_message_payload(system_text, text)
        seg = _suffix_seg(text)
        result = apply_safe_compression(
            payload,
            _segmentation_for([seg]),
            policy=CompressionConfig(enabled=False),
        )
        assert result.applied is False
        assert result.transformed_payload["messages"][1]["content"] == text
        assert result.transform_count == 0
