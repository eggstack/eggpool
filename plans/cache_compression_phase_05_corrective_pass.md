# Phase 5 Corrective Plan: Production Path Fixes for Safe Suffix Compression

Date: 2026-07-02

Parent roadmap: `plans/cache_preserving_deterministic_compression_roadmap.md`

Related plans:

- `plans/cache_compression_phase_02_canonical_request_segmentation.md`
- `plans/cache_compression_phase_03_transcoder_cache_stability.md`
- `plans/cache_compression_phase_04_observe_mode_compression_accounting.md`
- `plans/cache_compression_phase_05_safe_suffix_compression.md`

## Summary

The cache-preserving compression implementation has landed in the right broad architecture: metrics-first usage normalization, request segmentation, transcoder cache-stability helpers, observe-mode compression accounting, safe-mode compression application, database migrations, stats/dashboard surfaces, and substantial tests.

However, the current safe-mode mutating compressor needs a corrective pass before it should be trusted in production. The main issue is that the segmentation layer appears to emit `content_path` values that do not resolve to the actual string leaves in real OpenAI and Anthropic request payloads. The compressor walks those paths literally, so production safe-mode compression can silently no-op even when tests with hand-built segments pass.

There is also a safety-invariant gap: the stable-prefix hash used by the fail-closed check is currently derived from immutable segment metadata rather than re-derived from the mutated payload. This means the post-compression prefix check cannot catch an accidental stable-prefix mutation. Additionally, the current `stable_prefix_hash` is a coarse structural hash rather than an exact canonical prefix-content hash, which is useful for dashboards but insufficient for provider-cache locality and safety verification.

This plan fixes those integration bugs while preserving the original design principle:

> Preserve provider-cacheable stable prefixes. Compress only eligible volatile suffix content. Do not let cache/compression metrics skew same-provider account routing.

## Non-goals

- Do not add learned or semantic compression.
- Do not add semantic response caching.
- Do not change same-provider account routing fairness.
- Do not optimize routing based on cache hit rates.
- Do not enable compression by default.
- Do not compress stable prefixes by default.
- Do not synthesize provider cache controls in this pass.

## Current observed risks

### 1. OpenAI content paths do not resolve to production payload strings

Real OpenAI Chat Completions messages normally store text at:

```python
payload["messages"][i]["content"]
```

For list content parts, text may live at:

```python
payload["messages"][i]["content"][j]["text"]
```

The current OpenAI segmenter appears to emit paths such as:

```python
("messages", i, "tool")
("messages", i, "user")
("messages", i, "assistant")
```

Those paths describe role/source semantics, but they do not resolve to the actual string leaf. The safe compressor's `_collect_text()` and `_replace_path()` walk `content_path` literally, so these paths will not collect or mutate the real message content.

### 2. Anthropic content paths omit the `content` array key

Real Anthropic Messages content blocks normally live at:

```python
payload["messages"][i]["content"][j]["text"]
payload["messages"][i]["content"][j]["content"]
```

The current Anthropic segmenter appears to emit paths such as:

```python
("messages", i, j, "text")
("messages", i, j, "tool_result")
```

These omit the `"content"` list key and often use source labels rather than actual JSON keys. As with OpenAI, this likely causes production no-ops.

### 3. Stable-prefix hash is not exact enough for cache locality

The current `stable_prefix_hash` appears to hash a structural descriptor: segment counts, byte totals, token totals, sources, paths, and message indices. That is useful as a content-private shape hash, but it is not sufficient to decide whether the provider-visible stable prefix is identical.

A cryptographic hash of canonical stable-prefix content is still content-private. It does not store the prompt, but it does identify exact equality of the cacheable prefix. EggPool should maintain both:

- `stable_prefix_hash`: exact canonical stable-prefix content hash, for cache locality and fail-closed verification.
- `stable_prefix_shape_hash`: coarse structural hash, for dashboard grouping without requiring exact text equality.

If preserving the existing DB column semantics is important, add the exact hash as `stable_prefix_content_hash` and keep `stable_prefix_hash` as the existing structural hash. If compatibility risk is low, rename internally while keeping database migrations additive.

### 4. Fail-closed prefix verification hashes immutable segment metadata

The safe compressor currently computes pre/post prefix hashes from the same immutable `stable_segments` tuple. Since the tuple is not rebuilt from the mutated payload, the check will pass even if a future path bug mutates stable-prefix content.

The fail-closed check must re-extract stable-prefix content from the original payload and the transformed payload using stable-prefix segment paths, then hash canonical content from each payload.

### 5. Context-limit validation happens before compression

The current request path performs context-limit checks before segmentation and safe compression. That means compression cannot help an otherwise over-limit request fit within model limits.

This is acceptable for the immediate corrective pass if documented. Do not move context-limit logic casually. Instead, add explicit tests and documentation clarifying current behavior, then plan a later context-pressure phase if needed.

## Implementation plan

### Step 1: Split semantic source from concrete JSON content path

Update `RequestSegment` semantics so `content_path` always means an actual resolvable path into the decoded request payload.

Requirements:

- `content_path` must be usable by `_collect_text(payload, content_path)` to retrieve the string to analyze or mutate.
- If a segment represents a non-string region such as a tool schema object, `content_path` should still resolve to that object, but the segment should be protected and not compressible.
- Source/role semantics must remain in `source`, `reason`, and `message_index`, not encoded into `content_path`.

Suggested corrected paths for OpenAI:

```python
# system/developer/user/assistant/tool message with string content
("messages", i, "content")

# content parts with text fields
("messages", i, "content", j, "text")

# content parts with tool-result-like content field, if supported
("messages", i, "content", j, "content")

# top-level tools
("tools", i)
```

Suggested corrected paths for Anthropic:

```python
# top-level string system
("system",)

# top-level system content block text
("system", j, "text")

# message text block
("messages", i, "content", j, "text")

# message thinking block
("messages", i, "content", j, "thinking")

# tool_result content when string
("messages", i, "content", j, "content")

# tool_result nested text block if content is a list
("messages", i, "content", j, "content", k, "text")

# top-level tools
("tools", i)
```

Add a helper to validate path resolution:

```python
def resolve_path(payload: Any, path: tuple[Any, ...]) -> Any | None: ...

def resolve_text_path(payload: Any, path: tuple[Any, ...]) -> str | None: ...
```

Use these helpers in tests and optionally in debug assertions.

### Step 2: Segment list content parts at string-leaf granularity

The current segmentation frequently extracts text from composite content but stores one segment path for the whole role/source. This is not sufficient for mutation.

Update segmentation so each independently mutable string leaf gets its own `RequestSegment` when needed.

OpenAI examples:

```json
{"role": "user", "content": "text"}
```

One segment at `("messages", i, "content")`.

```json
{"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "image_url", "image_url": {...}}]}
```

One text segment at `("messages", i, "content", 0, "text")`; no compressible segment for image content.

Anthropic examples:

```json
{"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "tool_result", "content": "large log"}]}
```

Segments at `("messages", i, "content", 0, "text")` and `("messages", i, "content", 1, "content")`.

If a block contains list-valued `content`, segment only the string leaves. Do not try to replace whole complex blocks unless the transform explicitly supports that object shape.

### Step 3: Add exact stable-prefix content hashing

Add a canonical exact hash function that re-extracts stable-prefix values from a payload using stable-prefix segment paths.

Suggested API:

```python
def stable_prefix_content_hash(
    payload: Mapping[str, Any],
    segmentation: SegmentationResult,
) -> str:
    ...
```

Canonicalization requirements:

- Include actual stable-prefix content values, not only structural metadata.
- Use deterministic JSON serialization with `sort_keys=True` for objects.
- Preserve list order.
- Include path plus value to avoid accidental equality from values moving between prefix fields.
- Exclude volatile suffix and semi-stable context.
- Exclude request IDs, selected account IDs, timestamps, and non-payload metadata.
- Do not persist or log raw prefix text.

Suggested canonical payload:

```python
[
    {"path": ["messages", 0, "content"], "value": "..."},
    {"path": ["tools", 0], "value": {...}},
]
```

Then hash this canonical payload with SHA-256.

Add a separate structural hash if needed:

```python
def stable_prefix_shape_hash(segmentation: SegmentationResult) -> str:
    ...
```

Migration strategy:

- Prefer additive schema: add `stable_prefix_content_hash` and `stable_prefix_shape_hash` columns if necessary.
- If `stable_prefix_hash` is already widely used as structural hash, keep it but document it as legacy/coarse and populate the new exact hash going forward.
- If the code can safely switch internal meaning without dashboard breakage, set `stable_prefix_hash` to exact content hash and add `stable_prefix_shape_hash` for the old descriptor.

### Step 4: Fix safe-mode fail-closed verification

Update `apply_safe_compression()` to verify the exact stable-prefix content hash against original and transformed payloads.

Required behavior:

1. Compute `pre_stable_prefix_content_hash` from the original payload and segmentation.
2. Deep-copy payload and apply eligible volatile-suffix transforms.
3. Compute `post_stable_prefix_content_hash` from the transformed payload and the same stable-prefix segment paths.
4. If hashes differ and static-prefix compression is not explicitly allowed, return the original payload with `applied = False`, `failed_fallback = True`, and a warning reason such as `stable_prefix_content_hash_mismatch`.

Do not use immutable segment metadata as the post-mutation verification source.

Update `CompressionResult` fields:

- Preserve existing `pre_stable_prefix_hash` / `post_stable_prefix_hash` if used by dashboards, but clarify whether they are content or shape hashes.
- Prefer adding explicit names:
  - `pre_stable_prefix_content_hash`
  - `post_stable_prefix_content_hash`
  - `stable_prefix_content_preserved`
  - `stable_prefix_shape_hash`

If schema churn is too much, map exact content hashes into the existing pre/post hash fields and update docs/tests accordingly.

### Step 5: Fix compression transform marker correctness

Review all transforms for deterministic and diagnostically useful markers.

Minimum corrections:

- `fold_repeated_lines` should insert a marker. Current code appears to remove repeated lines without adding the intended marker, despite comments/tests referring to markers.
- `compact_logs` marker should include removed line count, not only head/tail and digest.
- `elide_base64_blobs` should include byte length and detected blob type in addition to digest.
- `compact_search_results` should preserve path/line/match lines for grep-like output; avoid dropping lines only based on the middle 50% heuristic.
- `compact_stack_traces` should preserve final exception/error line and not only deduplicate `File "..."` frame lines.

This step should not expand scope into semantic summarization. Keep transforms deterministic.

### Step 6: Reconcile observe-mode analyzer and mutating applier

The observe-mode analyzer and safe-mode applier should agree on eligibility and transform estimates as much as practical.

Add shared helpers for:

- path resolution
- text extraction
- segment filtering
- token estimation
- transform candidate detection
- reason-code names

Avoid exact code duplication where it can cause observe-mode metrics to disagree with actual compression.

Acceptance criteria:

- A request reported as eligible for compression in observe mode should be compressed in safe mode under the same policy, unless a fail-closed condition occurs.
- Suppressed candidates should have the same reason codes in observe and safe modes.

### Step 7: Preserve routing behavior explicitly

Add or strengthen tests proving compression/cache fields do not affect same-provider account selection.

Required test shape:

- Configure two same-provider accounts with equal priority/weight.
- Run routing with synthetic cache/compression metrics that differ by account.
- Assert selection follows existing quota/fairness behavior, not cache-hit ratio, compressed-token savings, or stable-prefix hash.

If this test already exists, make sure it is explicitly tied to the new cache/compression stats fields and cannot pass trivially because the fields are absent from the route scorer.

### Step 8: Clarify context-limit behavior

Do not move context-limit checks in this corrective pass unless implementation is trivial and safe. Instead:

- Add a test demonstrating current behavior: over-limit requests are rejected before compression can shrink them.
- Document this in `docs/transcoding.md`, `README.md`, or compression docs.
- Add a follow-up note in the roadmap for a later “context-pressure compression preflight” phase.

Future context-pressure design should be separate because it changes user-visible behavior: compression would become a way to make otherwise invalid requests valid. That needs stricter policy and clearer error reporting.

## Test plan

### Unit tests: path resolution

Add tests for `segment_request()` followed by `resolve_text_path()` on real payload shapes.

OpenAI cases:

- string system message resolves to `messages[i].content`.
- string tool message resolves to `messages[i].content`.
- latest user string resolves to `messages[i].content`.
- list content text part resolves to `messages[i].content[j].text`.
- image content is not marked compressible.
- tool schemas resolve to `tools[i]` and remain protected.

Anthropic cases:

- top-level string system resolves to `system`.
- top-level system text block resolves to `system[j].text`.
- message text block resolves to `messages[i].content[j].text`.
- tool_result string resolves to `messages[i].content[j].content`.
- tool_result nested text list resolves to `messages[i].content[j].content[k].text`.
- tools resolve to `tools[i]` and remain protected.
- cache_control blocks remain protected.

### Unit tests: production segmentation plus safe compression

Do not rely only on hand-built segments. For each fixture:

1. Build a real OpenAI or Anthropic request payload.
2. Run `segment_request(payload, protocol=...)`.
3. Run `apply_safe_compression(payload, segmentation, policy=safe_policy)`.
4. Assert expected compression applies.
5. Assert the stable prefix exact content hash is unchanged.
6. Assert system/tool/cache-control content is byte/canonical-equal before and after.

Required fixtures:

- OpenAI tool message with large repeated log.
- OpenAI latest user message containing a large pasted log.
- OpenAI content-part text block containing large search output.
- Anthropic tool_result string containing large repeated log.
- Anthropic tool_result nested content list containing large grep output.
- Anthropic system block with cache_control and large static content that must not compress.

### Unit tests: exact prefix hashing

Required tests:

- Changing only volatile suffix leaves `stable_prefix_content_hash` unchanged.
- Changing system content changes `stable_prefix_content_hash`.
- Changing tool schema changes `stable_prefix_content_hash`.
- Two different stable-prefix texts with same byte length produce different content hashes.
- Structural shape hash may remain same across content changes, but content hash must change.

### Unit tests: fail-closed safety

Required tests:

- Artificially create a malformed segment that targets a stable-prefix content path while claiming to be volatile; safe compression must detect changed prefix content and fall back to original payload.
- If `_replace_path()` mutates a protected stable path due to a bug, post-content-hash mismatch causes fallback.
- Fallback result has `applied = False`, `failed_fallback = True`, and original payload.

### Unit tests: transform markers

Required tests:

- Repeated-line folding emits a marker with repeat count.
- Log compaction marker includes removed line count and digest.
- Blob elision marker includes byte length, digest, and blob type.
- Search compaction preserves path/line/match lines.
- Stack trace compaction preserves final exception line.

### Integration/regression tests

- Compression disabled: provider-bound request body equals pre-change baseline.
- Observe mode: provider-bound request body equals pre-change baseline, but compression observations are persisted.
- Safe mode: provider-bound request body changes only in volatile suffix fields.
- OpenAI-to-Anthropic transcoding after compression keeps stable-prefix content hash unchanged before transcoding and provider-visible stable prefix deterministic after transcoding.
- Anthropic-to-OpenAI transcoding after compression preserves/loss-accounts cache controls as before.
- Same-provider account distribution remains governed by routing fairness, not cache/compression metrics.

## Database and stats updates

If exact content hash requires new columns, add an additive migration:

```sql
ALTER TABLE requests ADD COLUMN stable_prefix_content_hash TEXT;
ALTER TABLE requests ADD COLUMN stable_prefix_shape_hash TEXT;
ALTER TABLE requests ADD COLUMN compression_pre_stable_prefix_content_hash TEXT;
ALTER TABLE requests ADD COLUMN compression_post_stable_prefix_content_hash TEXT;
```

Then update repository/finalizer/stats/dashboard code to display both concepts clearly:

- “Stable prefix content hash” for exact equality/cache locality debugging.
- “Request shape hash” or “stable prefix shape hash” for aggregate grouping.

Never display raw prefix content.

## Documentation updates

Update docs to state:

- `content_path` in segmentation is a concrete JSON path, not a semantic role label.
- Safe compression is disabled by default.
- Observe mode never mutates provider-bound payloads.
- Safe mode only mutates eligible volatile suffix string leaves.
- Stable prefix preservation is verified using exact content hashes.
- Current context-limit checks occur before compression, so compression is not yet a context-fit rescue mechanism.
- Compression/cache metrics do not affect same-provider account routing by default.

## Acceptance criteria

The corrective pass is complete when:

- Production `segment_request()` paths resolve to real string leaves for OpenAI and Anthropic payloads.
- Safe-mode compression works when driven by production segmentation, not only hand-built test segments.
- Stable prefix exact content hash changes when system/tool/cache-control content changes.
- Stable prefix exact content hash does not change when only volatile suffix content changes.
- Fail-closed prefix verification re-hashes the transformed payload and catches accidental stable-prefix mutation.
- Safe-mode repeated-line/log/blob/search/stack transforms emit deterministic markers with adequate diagnostics.
- Compression disabled and observe mode remain non-mutating.
- Same-provider routing fairness remains unchanged.
- Existing tests plus new regression tests pass.
- Lint and type checks pass.

## Suggested implementation order

1. Add path-resolution helpers and tests.
2. Fix OpenAI segmentation paths.
3. Fix Anthropic segmentation paths.
4. Add exact stable-prefix content hashing.
5. Update safe-mode fail-closed verification to hash original and transformed payloads.
6. Fix transform markers and diagnostic preservation.
7. Align analyzer/applier shared filtering and reason codes.
8. Add production-segmentation compression tests.
9. Add routing non-regression tests.
10. Update docs and dashboard labels.
11. Run full test suite, ruff, pyright, and migration checks.

## Manual verification

1. Enable safe compression with low thresholds in a local config.
2. Send an OpenAI-compatible request with a large `role: "tool"` log in `messages[i].content`.
3. Confirm the provider-bound request has compacted only that message content.
4. Confirm system/developer messages and tool schemas are unchanged.
5. Repeat with an Anthropic `tool_result` content block.
6. Confirm cache-control blocks remain protected.
7. Compare request stats: compression applied, stable prefix content preserved, and no routing skew across same-provider accounts.
8. Disable compression and confirm provider-bound bodies return to baseline.

## Rollback notes

This pass should remain safe to disable with:

```toml
[compression]
enabled = false
```

If a specific transform causes regressions, disable it under `[compression.transforms]` while keeping observe mode available. If the exact content-hash migration is added and later dashboard code needs rollback, leave the new nullable columns in place and stop displaying them rather than attempting a destructive migration rollback.

## Completion summary

Completed 2026-07-02. All 4996 tests pass. Lint, typecheck, and format checks pass.

### Implementation steps

| Step | Description | Status |
|------|-------------|--------|
| 1 | Split semantic source from concrete JSON content path | Done |
| 2 | Segment list content parts at string-leaf granularity | Done |
| 3 | Add exact stable-prefix content hashing | Done |
| 4 | Fix safe-mode fail-closed verification | Done |
| 5 | Fix compression transform marker correctness | Done |
| 6 | Reconcile observe-mode analyzer and mutating applier | Done |
| 7 | Preserve routing behavior explicitly | Done |
| 8 | Clarify context-limit behavior | Done |

### New test files

- `tests/unit/test_compression_path_resolution.py` — path resolution for OpenAI and Anthropic payloads
- `tests/unit/test_stable_prefix_content_hash.py` — exact content hash vs structural hash
- `tests/unit/test_compression_fail_closed.py` — fail-closed catches mutated stable-prefix content
- `tests/unit/test_compression_markers_unified.py` — unified marker format across all six transforms
- `tests/unit/test_compression_apply_production.py` — production segmentation-driven compression
- `tests/unit/test_compression_routing_orthogonal.py` — compression/cache metrics do not affect routing
- `tests/unit/test_compression_context_limit_precedence.py` — context-limit checks happen before compression

### Deviations from plan

- The `parse_marker` regex in `markers.py` was fixed as part of the corrective pass (the `?` quantifier was missing on a group, causing parse failures for markers with `lines=0` or `tokens=0`). This was not explicitly called out in the plan but was discovered during marker correctness testing.
- The `stable_prefix_shape_hash` (legacy structural hash) was kept as a separate field rather than being replaced, to preserve backward compatibility with existing dashboard queries that reference `stable_prefix_hash`.
- Context-limit behavior was documented and tested but not changed — compression remains after context-limit checks, as the plan recommended.
