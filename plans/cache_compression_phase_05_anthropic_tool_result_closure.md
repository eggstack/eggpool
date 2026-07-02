# Phase 5 Closure Plan: Anthropic Tool-Result Path Resolution

Date: 2026-07-02

Parent roadmap: `plans/cache_preserving_deterministic_compression_roadmap.md`

Related plans:

- `plans/cache_compression_phase_05_safe_suffix_compression.md`
- `plans/cache_compression_phase_05_corrective_pass.md`

## Summary

The Phase 5 corrective pass substantially fixed safe suffix compression: OpenAI production paths now resolve to actual payload leaves, stable-prefix preservation is verified by re-hashing content from the transformed payload, markers are unified, and the new tests cover path helpers, exact prefix hashing, fail-closed behavior, context-limit precedence, and routing orthogonality.

One high-priority gap remains: Anthropic `tool_result` content-block segmentation still appears to emit a semantic path ending in `"tool_result"` instead of a concrete JSON path to the actual string leaf. The path-resolution tests correctly cover the concrete Anthropic shapes, but production segmentation must emit those same concrete paths. Until this is fixed, safe compression may silently no-op for Anthropic `tool_result` payloads even though OpenAI production compression is now likely working.

This plan closes that gap and tightens tests so the bug cannot recur.

## Problem statement

Real Anthropic `tool_result` content commonly appears in one of these shapes:

```json
{
  "type": "tool_result",
  "tool_use_id": "toolu_1",
  "content": "large tool output text"
}
```

or:

```json
{
  "type": "tool_result",
  "tool_use_id": "toolu_1",
  "content": [
    {"type": "text", "text": "large tool output text"}
  ]
}
```

The concrete mutable paths are therefore:

```python
("messages", message_index, "content", block_index, "content")
("messages", message_index, "content", block_index, "content", inner_index, "text")
```

The current Anthropic segmenter still appears to emit this for `block_type == "tool_result"`:

```python
("messages", message_index, "content", block_index, "tool_result")
```

That path does not exist in the real Anthropic payload. The compressor walks `content_path` literally, so `_collect_text()` returns `None`, `_replace_path()` never fires, and safe compression silently skips the segment.

## Non-goals

- Do not change routing.
- Do not add learned/semantic compression.
- Do not synthesize Anthropic cache controls.
- Do not change context-limit ordering.
- Do not broaden compression to stable prefixes.
- Do not change OpenAI path behavior except where shared helpers need tests.

## Implementation tasks

### 1. Fix Anthropic `tool_result` segmentation paths

Update `_segment_anthropic_message_block()` in `src/eggpool/transcoder/segmentation.py`.

For `block_type == "tool_result"`:

1. If `content` is a string, emit exactly one volatile suffix segment:

```python
RequestSegment(
    kind=SegmentKind.VOLATILE_SUFFIX,
    source=source,
    message_index=message_index,
    content_path=("messages", message_index, "content", block_index, "content"),
    byte_length=len(_serialize_for_hash(content_value_or_block)),
    estimated_tokens=_estimate_string_tokens(content_value),
    protected=False,
    compressible_candidate=True,
    reason="tool_result",
)
```

2. If `content` is a list, emit one segment per string text leaf:

```python
("messages", message_index, "content", block_index, "content", inner_index, "text")
```

Only emit a compressible segment when the nested block actually has string text. Ignore non-text nested blocks by default.

3. If `content` is a list containing dicts with a string `content` field, support that defensively as:

```python
("messages", message_index, "content", block_index, "content", inner_index, "content")
```

4. If `content` is absent, non-string, and non-list, return a conservative semi-stable or non-compressible segment only if needed for observability. Do not emit a bogus compressible path.

### 2. Allow one block to emit multiple segments

The current `_segment_anthropic_message_block()` appears to return a single `RequestSegment`. Nested `tool_result.content` lists require multiple mutable string-leaf segments.

Change the helper to return `list[RequestSegment]` rather than a single segment, or add a dedicated helper for tool-result block segmentation.

Suggested pattern:

```python
def _segment_anthropic_message_block(...) -> list[RequestSegment]:
    ...
```

Then update `_segment_anthropic_message()` to extend the segment list rather than collecting one segment per block directly.

If changing the return type is too invasive, add a `_segment_anthropic_tool_result_block()` helper that returns a list and let `_segment_anthropic_message()` branch before calling the single-block fallback.

### 3. Reuse path-resolution helpers in segmentation tests

For every emitted compressible Anthropic segment, assert:

```python
resolve_text_path(payload, segment.content_path) is not None
```

Add an invariant test that all `compressible_candidate=True` segments from `segment_request()` resolve to string leaves for both OpenAI and Anthropic production payloads.

This is the most important guardrail. It verifies that segmentation emits paths the compressor can actually use.

### 4. Strengthen production compression tests

The existing production tests for Anthropic tool-result compression should not allow `result.applied` to be false for an obviously compressible payload with permissive thresholds and transforms enabled.

Update or add tests:

#### `test_anthropic_tool_result_string_compresses_from_production_segmentation`

- Build a real Anthropic request with `tool_result.content` as a large repeated string.
- Run `segment_request(payload, protocol="anthropic")`.
- Assert at least one `volatile_suffix` segment resolves to the string content path.
- Run `apply_safe_compression(...)` with `fold_repeated_lines=True`, `compact_logs=False`, low thresholds.
- Assert:
  - `result.applied is True`.
  - `result.failed_fallback is False`.
  - `result.stable_prefix_preserved is True`.
  - `result.transformed_payload["system"]` is unchanged.
  - transformed `tool_result["content"]` is shorter and contains an EggPool marker.

#### `test_anthropic_tool_result_nested_text_compresses_from_production_segmentation`

- Build a real Anthropic request with `tool_result.content = [{"type": "text", "text": repeated_text}]`.
- Run the same pipeline.
- Assert the nested `text` field is compressed and marked.
- Assert non-text block fields such as `tool_use_id` are unchanged.

#### `test_anthropic_tool_result_non_text_nested_blocks_not_marked_compressible`

- Include a nested block without string text.
- Assert no compressible segment is emitted for that non-text block.

### 5. Tighten path-resolution tests to include emitted paths, not just helper examples

The existing `test_compression_path_resolution.py` validates that manually supplied paths resolve. Add tests that validate the paths actually emitted by `segment_request()`.

Examples:

```python
def test_segment_request_anthropic_tool_result_string_path_resolves() -> None:
    payload = {...}
    segmentation = segment_request(payload, protocol="anthropic")
    candidate_paths = [s.content_path for s in segmentation.segments if s.compressible_candidate]
    assert ("messages", 0, "content", 0, "content") in candidate_paths
    for path in candidate_paths:
        assert resolve_text_path(payload, path) is not None
```

Add equivalent nested-content test.

### 6. Update exact stable-prefix hash tests if needed

The Anthropic tool-result fix should not change stable-prefix content hashing, but add a regression test:

- Changing only Anthropic `tool_result.content` leaves `stable_prefix_content_hash()` unchanged.
- Changing Anthropic `system` text changes `stable_prefix_content_hash()`.

This confirms volatile suffix segmentation changes do not perturb cache-prefix identity.

### 7. Clarify naming around structural versus content hashes

The code now has both concepts:

- `SegmentationResult.stable_prefix_hash`: still structural/coarse, derived from `_stable_prefix_descriptor()`.
- `stable_prefix_content_hash(payload, segmentation)`: exact content hash, used by fail-closed verification.

Add or adjust comments/docs so future maintainers do not mistake `stable_prefix_hash` for the exact cache-content hash.

Minimum code-comment cleanup:

- In `SegmentationResult`, document `stable_prefix_hash` as the stable-prefix structural/shape hash if the field is left unchanged.
- In `_stable_prefix_descriptor()`, keep the current description but explicitly state that this is not sufficient for exact cache equality.
- In `apply.py`, clarify that `pre_shape_hash = segmentation.stable_prefix_hash` is used for dashboard/context only, while `pre_content_hash` / `post_content_hash` drive fail-closed behavior.

Avoid a schema migration unless there is already a clean reason to add a separate `stable_prefix_shape_hash` column. The priority here is the Anthropic production path bug.

### 8. Verify observe-mode analyzer behavior

The observe-mode analyzer may still classify Anthropic `tool_result` segments as candidates using the old segment paths. After fixing segmentation, confirm:

- Observe mode reports eligible candidates for Anthropic string `tool_result.content`.
- Safe mode applies compression for the same request under equivalent policy.
- Suppressed reason codes remain stable for protected/system/cache-control blocks.

If the analyzer uses only segment metadata and optional `text_hints`, it may not need code changes. But tests should verify the observe-to-safe agreement for at least one Anthropic tool-result payload.

## Test plan

Run targeted tests first:

```bash
pytest tests/unit/test_compression_path_resolution.py
pytest tests/unit/test_compression_apply_production.py
pytest tests/unit/test_stable_prefix_content_hash.py
pytest tests/unit/test_compression_fail_closed.py
```

Then run the broader compression subset:

```bash
pytest tests/unit/test_compression_*.py tests/unit/test_stable_prefix_content_hash.py
```

Then full validation:

```bash
pytest
ruff check .
pyright
```

Use the repo's actual lint/type commands if they differ from the above.

## Acceptance criteria

- Anthropic string `tool_result.content` emits a concrete path ending in `"content"` and resolves with `resolve_text_path()`.
- Anthropic nested `tool_result.content[].text` emits concrete paths ending in `"text"` and resolves with `resolve_text_path()`.
- Safe compression applies to obviously compressible Anthropic tool-result string payloads under permissive safe policy.
- Safe compression applies to obviously compressible Anthropic nested tool-result text payloads under permissive safe policy.
- All affected tests assert `result.applied is True` for those obviously compressible Anthropic cases; no more `if result.applied:` escape hatch for the target bug.
- System, tools, cache-control, and thinking blocks remain unchanged.
- Stable-prefix exact content hash is unchanged when only Anthropic tool-result content changes.
- Stable-prefix exact content hash changes when Anthropic system content changes.
- Same-provider routing behavior remains unaffected.
- Compression disabled and observe mode remain non-mutating.
- Full test/lint/type suite passes.

## Manual verification

1. Enable safe compression locally with low thresholds.
2. Send an Anthropic-compatible request whose latest user message contains a `tool_result` block with a large repeated string in `content`.
3. Confirm the provider-bound request contains a compressed `tool_result.content` string with an EggPool marker.
4. Confirm `system`, `tools`, `cache_control`, `tool_use_id`, and `thinking` blocks are unchanged.
5. Repeat with nested `tool_result.content = [{"type": "text", "text": "..."}]`.
6. Confirm stats record compression applied and stable-prefix preserved.
7. Confirm account selection remains governed by existing routing fairness.

## Rollback notes

This is a narrow segmentation correction. If it causes unexpected behavior, safe compression can still be disabled globally:

```toml
[compression]
enabled = false
```

If only Anthropic tool-result compression is problematic, temporarily avoid marking Anthropic `tool_result` nested content as `compressible_candidate` while preserving the path-resolution helpers and exact prefix hash machinery.
