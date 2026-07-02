# Phase 5 Plan: Safe Suffix-Only Deterministic Compression

Date: 2026-07-01

Parent roadmap: `plans/cache_preserving_deterministic_compression_roadmap.md`

## Corrective pass completed (2026-07-02)

The corrective pass in `plans/cache_compression_phase_05_corrective_pass.md`
has been completed. All 4996 tests pass. The following corrections were
applied to the production implementation:

- **Concrete JSON content paths**: Segmentation `content_path` values now
  resolve to actual string leaves of the request payload (not semantic
  role labels). OpenAI paths use `("messages", i, "content")` for string
  content, `("messages", i, "content", j, "text")` for list content parts.
  Anthropic paths use `("system",)` for string system,
  `("system", j, "text")` for system blocks,
  `("messages", i, "content", j, ...)` for content blocks.
- **Exact stable-prefix content hash**: `stable_prefix_content_hash` is an
  exact SHA-256 of canonical stable-prefix content (system, tools,
  cache_control blocks), re-extracted from the payload via stable-prefix
  segment paths. The legacy structural hash is tracked separately as
  `stable_prefix_shape_hash`.
- **Fail-closed re-verifies mutated payload**: The fail-closed verification
  re-hashes the TRANSFORMED payload's stable-prefix content (not just
  immutable segment metadata), catching real path bugs that mutate
  stable-prefix content.
- **Unified marker format**: All six transforms emit markers via
  `eggpool.transcoder.compression.markers.build_marker` with the format
  `[EggPool compression: <transform> | segment=<id> | lines=<n> |
  tokens=<n> | sha256=<digest>]`.
- **Tests added/updated**: `test_compression_path_resolution.py`,
  `test_stable_prefix_content_hash.py`, `test_compression_fail_closed.py`,
  `test_compression_markers_unified.py`,
  `test_compression_apply_production.py`,
  `test_compression_routing_orthogonal.py`,
  `test_compression_context_limit_precedence.py`.

## Goal

Implement the first request-mutating deterministic compressor. The compressor should apply only to eligible volatile suffix segments by default and must preserve stable provider-cacheable prefixes exactly or canonically. This phase turns observe-mode candidates into safe transformations for logs, repeated output, search results, stack traces, blobs, and machine-generated payloads.

The guiding invariant is:

```text
safe compression enabled
=> stable prefix unchanged
=> protected cache boundaries respected
=> only eligible volatile suffix content may change
=> all changes are auditable
```

## Non-goals

- Do not compress system/developer instructions by default.
- Do not compress tool schemas by default.
- Do not compress provider-native cache-control regions.
- Do not add learned/semantic compression.
- Do not route based on compression or cache metrics.
- Do not summarize content with an LLM.
- Do not mutate content when estimated savings are below policy thresholds.

## Configuration behavior

This phase activates mutating behavior only when explicitly configured.

Recommended default remains:

```toml
[compression]
enabled = false
mode = "observe"
```

Safe mutating mode:

```toml
[compression]
enabled = true
mode = "safe"
placement = "suffix_only"
respect_cache_boundaries = true
compress_static_prefix = false
min_candidate_tokens = 2048
min_savings_tokens = 1024
max_compression_latency_ms = 25
```

Request-level overrides should be added if the project already has a clean header/config override pattern. Suggested headers:

- `x-eggpool-compression: off`
- `x-eggpool-compression: observe`
- `x-eggpool-compression: safe`
- `x-eggpool-cache-policy: preserve`

If header support would be too invasive for this phase, document it as a follow-up and add internal hooks now.

## Implementation tasks

### 1. Compression application framework

Extend the phase 4 candidate framework with a mutating application step.

Requirements:

- Apply transforms only to candidates that passed policy filtering.
- Revalidate candidate segment location immediately before mutation.
- Preserve request structure and provider protocol validity.
- Record pre/post estimates and reason codes.
- Keep stable prefix hash unchanged after compression.
- Fail safely: if any transform fails, leave that segment unchanged and record a warning.

### 2. Stable elision marker format

Define a deterministic model-visible marker for removed content. The marker should be concise, stable, and auditable.

Example:

```text
[EggPool compression: log output compacted.
Original lines: 18420. Preserved first 120, last 240, and 318 diagnostic lines.
Removed lines: 17742. Original SHA256: <hex>.]
```

Marker requirements:

- No timestamps.
- No selected account/provider details unless already visible to the model.
- No random IDs.
- Include enough detail to tell the model that information was omitted.
- Include digest for debugging/reproducibility without storing raw content.
- Avoid putting markers in stable prefix regions.

### 3. Repeated-line folding transform

Transform exact repeated adjacent line runs in eligible volatile suffix text.

Behavior:

- Preserve one representative line.
- Replace the rest with a count marker.
- Preserve surrounding context.
- Use conservative thresholds.

Example:

```text
same warning line
same warning line
same warning line
...
```

becomes:

```text
same warning line
[EggPool compression: previous line repeated 127 additional times.]
```

Tests:

- Exact repeated lines fold.
- Non-adjacent lines are not folded in this first implementation unless explicitly supported.
- Short repeats below threshold remain unchanged.

### 4. Long log/command-output compaction

Compact very large tool/log segments while preserving diagnostically useful material.

Preserve:

- Command text if present.
- Exit code if present.
- First N lines.
- Last N lines.
- Lines matching diagnostic patterns.
- Stack trace heads/tails.
- Test names and failure summaries when detectable.

Diagnostic patterns should include common case-insensitive tokens such as:

- `error`
- `failed`
- `failure`
- `panic`
- `exception`
- `traceback`
- `assert`
- `expected`
- `actual`
- `denied`
- `not found`
- `timeout`
- `segfault`
- `warning` only when volume is bounded

Keep pattern matching deterministic and cheap.

### 5. Stack trace compaction

Compact repeated stack frames and repeated exception blocks.

Rules:

- Preserve the first occurrence of each unique trace shape.
- Preserve the final exception/error line.
- Preserve file paths, line numbers, and function names.
- Fold repeated identical frames with count markers.
- Avoid aggressive deduplication that loses the final active error path.

### 6. Search-result compaction

Compact large grep/ripgrep/search result output.

Rules:

- Preserve file path and line number for each retained match.
- Preserve matched lines.
- Limit excessive context lines.
- Collapse duplicate matches.
- Include marker with original match count and retained match count.

This is especially useful for coding agents because search tools often flood prompts with repetitive context.

### 7. Blob/base64 elision

Elide large opaque blobs in eligible volatile suffixes.

Replacement should include:

- Detected blob type if obvious (`base64`, `data_uri`, `opaque_high_entropy_text`).
- Original byte length.
- SHA256 digest.
- Small prefix/suffix only if useful and safe.

Do not elide small strings. Do not elide content in stable prefix. Do not elide code blocks just because they are long unless they match blob heuristics.

### 8. Machine JSON minification

For eligible volatile suffix JSON payloads:

- Parse with size/time bounds.
- Re-serialize without insignificant whitespace.
- Preserve object key order.
- Do not sort keys unless the input already sorts them and code can guarantee semantic stability.
- If parsing fails, leave unchanged.

This transform should be disabled for stable prefix and for user-authored code snippets.

### 9. Request mutation integration

Apply compression before provider dispatch and before final provider-specific serialization when possible. If the current pipeline requires provider-specific mutation after transcoding, ensure prefix hash comparisons are still possible.

Recommended order:

1. Parse inbound request.
2. Segment canonical request.
3. Estimate cache/protected boundaries.
4. Analyze compression candidates.
5. Apply safe compression to eligible volatile suffix segments.
6. Recompute post-compression estimates.
7. Route according to existing fairness/quota behavior.
8. Transcode/serialize deterministically.
9. Dispatch.

If current code routes before the most natural compression location, do not rewrite routing in this phase unless necessary. The main requirement is no routing skew from compression/cache metrics.

### 10. Metadata and persistence

Persist mutating compression metadata:

- compression mode
- compression applied boolean
- transform count
- transforms applied by reason code
- original candidate tokens estimate
- compressed candidate tokens estimate
- estimated savings tokens
- original stable prefix hash
- post-compression stable prefix hash
- stable prefix preserved boolean
- warnings
- compression latency ms

If stable prefix hash changes unexpectedly, fail closed for compression: send the uncompressed request and log a high-severity warning.

### 11. Safety checks

Add explicit runtime assertions or defensive checks:

- No transform may target `stable_prefix` when `compress_static_prefix = false`.
- No transform may target `protected = true` when `respect_cache_boundaries = true`.
- Post-compression stable prefix hash must match pre-compression stable prefix hash.
- Compression latency budget exceeded should stop further transforms and preserve remaining content.
- Invalid transformed request should fall back to original request.

### 12. Tests

Required tests:

- Safe mode mutates eligible volatile suffix logs.
- Safe mode preserves stable prefix hash.
- Safe mode does not mutate system/developer messages.
- Safe mode does not mutate tool schemas.
- Safe mode does not mutate Anthropic cache-control protected blocks.
- Repeated lines are folded with deterministic markers.
- Long logs preserve first/last/diagnostic lines.
- Stack traces preserve final exception and useful frames.
- Search result compaction preserves file paths and line numbers.
- Blob elision includes byte length and digest.
- JSON minification preserves parsed semantic content and key order.
- Compression disabled produces identical outbound request to baseline.
- Observe mode still does not mutate.
- Header override `off`, if implemented, disables mutation.
- Transform failure falls back to original segment.
- Unexpected stable prefix hash change disables compression for that request.
- Same-provider account routing remains fairness/quota-first despite compression metrics.

### 13. Replay fixtures

Add realistic fixtures if possible:

- Large pytest failure output.
- Rust `cargo test`/`cargo check` output.
- Python traceback loops.
- ripgrep output over many files.
- npm/pnpm/pip install noise.
- base64 image/blob accidentally pasted into tool output.
- large JSON API response.

Fixtures should assert transformed output shape, preserved diagnostics, and stable prefix preservation.

## Acceptance criteria

- Safe compression can be enabled explicitly.
- Only eligible volatile suffix content is mutated by default.
- Stable prefix hashes remain unchanged after compression.
- Provider-bound request bodies remain valid for OpenAI and Anthropic paths.
- Compression metadata is persisted and visible in stats.
- Compression failure falls back to original content rather than breaking requests.
- Existing routing/account fairness behavior remains unchanged.

## Manual verification

1. Enable safe compression.
2. Send a request with a stable long prefix and a huge repeated latest tool output.
3. Confirm provider-bound stable prefix is unchanged.
4. Confirm volatile suffix is compacted with deterministic markers.
5. Confirm stats show estimated savings and applied transforms.
6. Repeat with compression disabled and verify provider receives original content.
7. Repeat with two same-provider accounts and confirm account distribution is still governed by existing routing fairness.
8. Send a request with large content inside tool schema/system prefix and confirm it is not compressed.

## Rollback notes

The feature should be easy to disable by setting:

```toml
[compression]
enabled = false
```

If a specific transform causes issues, operators should be able to disable that transform while keeping observe mode or other transforms. Because all mutations are pre-dispatch and bounded, rollback should not require database migration rollback.
