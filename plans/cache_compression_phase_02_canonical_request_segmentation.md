# Phase 2 Plan: Canonical Request Segmentation

Date: 2026-07-01

Parent roadmap: `plans/cache_preserving_deterministic_compression_roadmap.md`

## Goal

Add a structural segmentation layer that annotates canonical requests into cache/compression regions without mutating the request. The segmentation layer gives later phases a safe way to preserve provider-cacheable stable prefixes while identifying volatile suffixes that can be deterministically compressed.

This phase should answer:

- Which part of the request is stable prefix material?
- Which part is semi-stable rolling conversation/context?
- Which part is volatile suffix material such as latest tool output or logs?
- What is the estimated token volume of each segment?
- What hash identifies the stable prefix for cache-locality metrics?

## Non-goals

- Do not compress or rewrite request content.
- Do not synthesize cache controls.
- Do not change provider routing.
- Do not require tokenizer-perfect token counts.
- Do not use learned semantic classification.

## Core concept

Every supported inbound request should be convertible into an annotated canonical structure with ordered segments:

```text
stable_prefix:
  system/developer instructions
  tool schemas
  provider-native cache blocks
  persistent project rules

semi_stable_context:
  prior turns
  selected file snippets
  repo summaries
  current task state

volatile_suffix:
  latest user turn
  latest tool result
  command output
  test logs
  grep/search output
  generated blobs
```

The segmentation should be conservative. If classification is uncertain, prefer `semi_stable_context` over `volatile_suffix`, and prefer preserving content over marking it as compressible.

## Implementation tasks

### 1. Identify canonical request layer

Find the best existing layer to attach annotations. Candidate locations include:

- request models for OpenAI Chat Completions
- request models for Anthropic Messages
- transcoder canonical structures if they already exist
- route preparation code before provider-specific serialization

Do not create a parallel parser if a canonical representation already exists. Add annotations near the structure that is already used by routing/transcoding.

### 2. Define segment data structures

Add typed structures similar to:

```python
SegmentKind = Literal["stable_prefix", "semi_stable_context", "volatile_suffix"]
SegmentSource = Literal[
    "system",
    "developer",
    "tool_schema",
    "cache_control",
    "prior_message",
    "latest_user_message",
    "tool_result",
    "command_output",
    "search_result",
    "blob",
    "unknown",
]

class RequestSegment:
    kind: SegmentKind
    source: SegmentSource
    message_index: int | None
    content_path: tuple[str | int, ...]
    byte_length: int
    estimated_tokens: int | None
    protected: bool
    compressible_candidate: bool
    reason: str
```

The exact representation can vary, but it must preserve enough location information for later phases to apply compression without reclassifying the request.

### 3. Add conservative token estimation

Use the existing token-estimation machinery if present. If exact provider tokenizers are not available, add a cheap estimate with explicit status.

Requirements:

- Token estimates are for metrics and thresholds only.
- Estimates must not be used as exact billing counters.
- Segment-level estimates should be cheap enough for SBC deployments.
- Missing estimates should not fail request handling.

### 4. Segment OpenAI Chat Completions requests

Implement segmentation for OpenAI-compatible request bodies.

Suggested rules:

- `messages` before the latest volatile message are usually `stable_prefix` or `semi_stable_context` depending on role and position.
- `system` and `developer` role messages near the beginning are `stable_prefix` and protected.
- Tool/function schemas are `stable_prefix` and protected.
- Client-supplied cache-key or provider extension fields, if any, should be protected metadata.
- The latest `user` message is usually `volatile_suffix` unless it contains only short instruction text.
- Tool role messages and large tool-result-like content near the tail are `volatile_suffix`.
- Large content blocks with log markers, stack traces, grep output, or command output patterns are `volatile_suffix` candidates.
- Ambiguous prior assistant/user turns are `semi_stable_context`.

### 5. Segment Anthropic Messages requests

Implement segmentation for Anthropic-compatible request bodies.

Suggested rules:

- Top-level `system` is `stable_prefix` and protected.
- `tools` are `stable_prefix` and protected.
- Content blocks with `cache_control` are protected and should define or refine cache boundaries.
- Earlier message blocks are `semi_stable_context` unless clearly system/tool/schema/static.
- Recent tool results and large text blocks near the end are `volatile_suffix`.
- Preserve thinking/reasoning-related fields as protected unless current code already treats them differently.

### 6. Stable prefix hashing

Compute a stable prefix hash for observability. This hash should reflect the provider-visible/canonical stable prefix after parsing but before compression.

Requirements:

- Deterministic for identical stable prefixes.
- Excludes volatile suffix content.
- Does not include request timestamp, request ID, selected account, latency, or other unstable metadata.
- Does not expose prompt content directly in logs or dashboard.

Suggested output:

- `stable_prefix_hash`
- `stable_prefix_bytes`
- `stable_prefix_estimated_tokens`
- `segment_count_by_kind`

### 7. Request shape hashing

Add a broader request shape hash that includes structural classification without full content disclosure.

Potential fields:

- provider/client protocol
- model
- role sequence
- content block type sequence
- approximate segment token buckets
- tool schema count
- presence of cache controls
- volatile suffix classifier type

This can be used later for aggregate metrics without storing prompt text.

### 8. Persist segmentation summary

Extend the phase 1 storage placeholders if present:

- stable prefix hash
- request shape hash
- estimated stable prefix tokens
- estimated semi-stable tokens
- estimated volatile suffix tokens
- segment classification status

Do not persist raw message content.

### 9. Expose diagnostics

Add debug-level logs and stats fields showing segmentation summary.

Examples:

```text
stable_prefix_tokens_est=48000 semi_stable_tokens_est=12000 volatile_tokens_est=9000 stable_prefix_hash=...
```

Dashboard display may be minimal in this phase. The primary requirement is that later compression metrics can use the segmentation data.

### 10. Tests

Required tests:

- OpenAI system/developer/tool schemas classify as protected stable prefix.
- Anthropic top-level system/tools/cache-control blocks classify as protected stable prefix.
- Latest large tool output classifies as volatile suffix.
- Prior mixed conversation turns classify as semi-stable context.
- Unknown/ambiguous content is not marked as compressible by default.
- Identical stable prefixes produce identical stable prefix hashes.
- Changing only the volatile suffix does not change stable prefix hash.
- Changing system/tool schema content changes stable prefix hash.
- Segmentation does not mutate outbound request body.
- Existing request handling and transcoding tests still pass.

## Acceptance criteria

- Every supported request path can produce segmentation summary metadata.
- Stable prefix hashes are deterministic and content-private.
- Large volatile tool/log/search content is identified as a future compression candidate.
- Protected stable prefix content is never marked as compressible by default.
- No request or routing behavior changes in this phase.

## Manual verification

1. Send repeated requests with identical system/tool schema prefixes and different latest user messages.
2. Confirm the stable prefix hash remains unchanged.
3. Send a request with modified tool schema.
4. Confirm the stable prefix hash changes.
5. Send a request with a large tool-output tail.
6. Confirm the volatile suffix estimate increases and the segment is marked as a candidate.
7. Confirm provider-visible request bodies remain identical to pre-phase behavior.

## Rollback notes

Segmentation should be observational. If any issue appears, disable persistence/dashboard use of segmentation metadata and leave the parser annotations unused. No provider-facing behavior should need rollback if the phase is implemented correctly.
