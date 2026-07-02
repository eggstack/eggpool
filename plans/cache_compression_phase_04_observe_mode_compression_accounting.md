# Phase 4 Plan: Observe-Mode Deterministic Compression Accounting

Date: 2026-07-01

Parent roadmap: `plans/cache_preserving_deterministic_compression_roadmap.md`

## Goal

Add observe-mode deterministic compression analysis. In this phase EggPool detects and measures compression opportunities but never mutates outbound requests. The output is metrics, warnings, reason codes, and operator visibility.

This phase should answer:

- How many tokens could safe deterministic compression likely save?
- Which request regions are candidates?
- Which candidates were suppressed because they overlap cache-protected stable prefix material?
- How much analyzer latency does the feature add on SBC-class hardware?
- Which transform families are likely to matter for real coding-agent traffic?

## Non-goals

- Do not mutate request bodies.
- Do not compress stable prefixes.
- Do not add learned/model-based compression.
- Do not change routing.
- Do not synthesize provider cache controls.

## Configuration

Add the initial `[compression]` config section. Defaults should be safe and non-mutating.

```toml
[compression]
enabled = false
mode = "observe" # observe | safe | balanced
placement = "suffix_only" # suffix_only | after_cache_boundary | anywhere
respect_cache_boundaries = true
compress_static_prefix = false
min_candidate_tokens = 2048
min_savings_tokens = 1024
max_compression_latency_ms = 25

[compression.transforms]
fold_repeated_lines = true
compact_logs = true
compact_search_results = true
elide_base64_blobs = true
minify_machine_json = true
compact_stack_traces = true
```

Important semantics:

- `enabled = false` means no analyzer work.
- `enabled = true` and `mode = "observe"` means analyze and record only.
- `respect_cache_boundaries = true` means protected stable-prefix segments are never candidates.
- `placement = "suffix_only"` means only volatile suffix segments are considered.

## Implementation tasks

### 1. Add compression policy model

Create typed config models for compression settings. Validation rules:

- Unknown mode values fail config validation.
- `compress_static_prefix = true` should require a non-default mode or explicit override, even if later phases use it.
- `max_compression_latency_ms` must be non-negative.
- `min_candidate_tokens` and `min_savings_tokens` must be non-negative.
- Transform toggles default to true only when compression is enabled; no analyzer should run when disabled.

### 2. Define compression candidate model

Add an internal candidate/result structure.

Suggested fields:

```python
class CompressionCandidate:
    segment_id: str
    segment_kind: str
    source: str
    protected: bool
    transform: str
    original_bytes: int
    estimated_original_tokens: int | None
    estimated_compressed_tokens: int | None
    estimated_savings_tokens: int | None
    eligible: bool
    suppressed_reason: str | None
    reason_codes: list[str]
```

Per-request summary:

```python
class CompressionObservation:
    mode: Literal["observe"]
    candidate_count: int
    eligible_candidate_count: int
    suppressed_candidate_count: int
    estimated_original_tokens: int | None
    estimated_compressed_tokens: int | None
    estimated_savings_tokens: int | None
    analyzer_latency_ms: float
    warnings: list[str]
```

### 3. Candidate detection framework

Implement analyzer functions that inspect phase 2 segments and return candidates. The framework should be deterministic, fast, and side-effect free.

Candidate families:

- Repeated line runs.
- Long logs/command output.
- Stack traces and repeated stack frames.
- Grep/ripgrep/search result output.
- Base64 or opaque blob content.
- Machine-generated JSON-like payloads.
- Vendor/generated/lockfile-like content when structurally recognizable.

The analyzers should not require perfect detection. False negatives are acceptable in observe mode; false positives should be tracked and suppressed if protected.

### 4. Repeated-line analyzer

Detect repeated exact or near-exact adjacent lines in volatile suffix segments.

Initial conservative behavior:

- Exact repeated adjacent lines only.
- Minimum run length threshold, for example 5 lines.
- Estimate compressed representation as one representative line plus count marker.
- Do not inspect protected segments unless only counting suppressed opportunities.

Reason codes:

- `repeated_line_run`
- `below_min_candidate_tokens`
- `protected_cache_boundary`

### 5. Log/command-output analyzer

Detect large text blocks that look like logs or command output.

Signals:

- Many newline-separated lines.
- ANSI escape codes.
- timestamps/log levels.
- repeated prefixes.
- test runner output.
- package manager progress/noise.
- compiler/test failure markers.

Observation estimate:

- Preserve first N lines, last N lines, and error-matching lines.
- Estimate removed middle lines.
- Do not actually build the compressed text in this phase unless needed for accurate estimates.

### 6. Search-result analyzer

Detect ripgrep/grep-like output and large search listings.

Signals:

- `path:line:match` patterns.
- repeated file paths.
- many adjacent context lines.
- duplicate matches.

Observation estimate:

- Preserve path, line number, match line.
- Reduce duplicate or excessive surrounding context.

### 7. Blob/base64 analyzer

Detect opaque material that is almost never useful raw in a prompt.

Signals:

- Long high-entropy lines.
- Base64-like alphabet and padding.
- data URI prefixes.
- minified binary-like escaped blobs.

Observation estimate:

- Replace with digest/byte count/content class in future phase.
- For observe mode, estimate the token savings if elided.

### 8. Machine JSON analyzer

Detect large machine-generated JSON blocks where whitespace-only minification would save tokens/bytes without changing semantics.

Rules:

- Only consider valid JSON blocks when cheap to parse within size limits.
- Do not reorder object keys.
- Do not minify user-authored code snippets in stable prefix.
- Suppress if parse cost would exceed latency budget.

### 9. Cache-boundary suppression

Every candidate must pass through policy filtering:

- If segment is protected and `respect_cache_boundaries = true`, suppress.
- If segment kind is `stable_prefix` and `compress_static_prefix = false`, suppress.
- If placement is `suffix_only` and segment kind is not `volatile_suffix`, suppress.
- If estimated savings below threshold, suppress.
- If analyzer latency budget exceeded, stop further analysis and record warning.

### 10. Persistence and stats

Persist per-request summary, not raw content. Suggested fields:

- compression mode
- compression candidate count
- eligible candidate count
- suppressed candidate count
- estimated original candidate tokens
- estimated compressed candidate tokens
- estimated savings tokens
- analyzer latency ms
- warning count
- reason-code summary if storage format supports it

Dashboard/API aggregation:

- total estimated compressible tokens
- total suppressed due to cache boundary
- top reason codes
- median/p95 analyzer latency
- candidate requests by provider/account/model/client protocol

### 11. Tests

Required tests:

- Observe mode never changes request body.
- Disabled compression runs no analyzers.
- Repeated-line candidate is detected in volatile suffix.
- Large log candidate is detected in volatile suffix.
- Search-result candidate is detected in volatile suffix.
- Base64/blob candidate is detected in volatile suffix.
- Protected stable-prefix candidates are suppressed.
- Semi-stable candidates are suppressed under `suffix_only`.
- Savings below threshold are suppressed.
- Latency budget warnings are recorded and analysis stops cleanly.
- Stats aggregation reports unknown/null estimates correctly.

## Acceptance criteria

- Operators can enable observe mode without changing provider-bound payloads.
- Compression opportunity is visible by request/provider/account/model.
- Cache-protected suppression is visible and separate from eligible savings.
- Analyzer overhead is bounded and measured.
- Existing routing behavior is unchanged.

## Manual verification

1. Enable `[compression] enabled = true`, `mode = "observe"`.
2. Send a normal short request and confirm no mutation and low/no candidates.
3. Send a request with a large repeated log in the latest tool output.
4. Confirm eligible candidate metrics increase.
5. Send a request with repeated content in system/tool schema prefix.
6. Confirm candidates are suppressed due to cache boundary.
7. Confirm provider-bound body matches body with compression disabled.
8. Confirm account distribution remains governed by existing routing behavior.

## Rollback notes

If analyzer overhead or false-positive metrics are problematic, set `[compression] enabled = false`. Since observe mode does not mutate traffic, rollback should only require disabling config and optionally hiding dashboard metrics.
