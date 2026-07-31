# Deep Dive: Protocol Transcoding

Back to [Overview](overview.md)

## Purpose

Transparently translates request/response bodies between OpenAI and Anthropic protocols. When a client sends Anthropic-format requests but the routed provider only speaks OpenAI (or vice versa), the transcoder bridges the gap.

## Architecture

```
Client Protocol          Upstream Protocol
─────────────            ─────────────────
OpenAI Chat    ───────►  Anthropic Messages
Anthropic Msg  ───────►  OpenAI Chat
OpenAI Chat    ───────►  OpenAI Chat (passthrough)
Anthropic Msg  ───────►  Anthropic Msg (passthrough)
```

## Key Modules

### `transcoder/protocol.py`

`select_transcoder()` — single source of truth for dispatch. Returns the appropriate `BodyTranscoder` based on client vs upstream protocol.

### `transcoder/openai_to_anthropic.py`

`OpenAIToAnthropic` — body transcoder (OpenAI → Anthropic):
- `encode_request()`: translates OpenAI request body to Anthropic format
- `decode_response()`: translates Anthropic response back to OpenAI
- Handles tools, tool_choice, parallel_tool_calls
- Handles reasoning_effort → thinking budget translation
- Records cache boundary annotations and loss warnings

### `transcoder/anthropic_to_openai.py`

`AnthropicToOpenAI` — body transcoder (Anthropic → OpenAI):
- `encode_request()`: translates Anthropic request body to OpenAI format
- `decode_response()`: translates OpenAI response back to Anthropic
- Drops unsupported `cache_control` annotations
- Maps thinking blocks to reasoning_content

### `transcoder/streaming.py`

Streaming transcoder implementations for both directions:
- `OpenAIToAnthropicStreaming`
- `AnthropicToOpenAIStreaming`

Phase 9 optimization: one shared bounded decoder, synchronous
`translate_frame()`/`finish()` (no per-chunk `await`), compact JSON separators
`(",", ":")`, and coalesced output chunks.

### `transcoder/policy.py`

`TranscoderPolicy` — configuration and per-request state:
- Feature flags per transcoding capability
- Reasoning field names for OpenAI compatibility
- Loss policy (warn/reject)
- Budget resolution settings

### `transcoder/context.py`

`TranscodeContext` — per-request transcoding state dataclass carrying loss warnings, cache boundary tracker, tool-call ID map, and upstream protocol.

### `transcoder/static_headers.py`

Protocol-required static headers for cross-protocol transcoding (e.g. `anthropic-version` for Anthropic upstreams).

### `transcoder/prepared.py`

`PreparedTranscode` — reusable preflight with mutable diagnostics.

### `transcoder/budget_resolver.py`

`resolve_thinking_budget()` — single source of truth for effort-to-budget translation:
1. Explicit `thinking.budget_tokens` (Anthropic style)
2. `reasoning_effort` via `ThinkingCapability.effort_to_budget_tokens`
3. `[transcoder.thinking_budget_defaults]`
4. Hard-coded fallback (low=1024, medium=4096, high=16384)

Budgets clamped to `budget_tokens_min`/`budget_tokens_max`. `strict` policy rejects unknown efforts.

### `transcoder/ids.py`

`ToolCallIdMap` — per-request tool-call ID namespace mapping. Mints `call_<24 hex>` and `toolu_<24 hex>` IDs so the two namespaces never collide.

### `transcoder/usage.py`

Usage canonicalization across protocols (input_tokens ↔ prompt_tokens, cache counters, etc.).

### `transcoder/errors.py`

`TranscodeLossError` — raised when `loss_policy = "reject"` and protected cache-control loss kinds are detected.

### `transcoder/segmentation.py`

`segment_request()` — stable-prefix/semi-stable/volatile segmentation. Observational only — never mutates request bodies.

### `transcoder/segmentation_guard.py`

`should_segment_request()` — skip segmentation when no features are active.

### `transcoder/cache_stability.py`

`CacheBoundaryTracker` — records what the transcoder did to `cache_control` annotations during translation. Append-only, bounded (64 annotations/request).

### `transcoder/cache_synthesis.py` / `cache_synthesis_policy.py`

Phase 9 synthetic cache controls: optional provider-bound `cache_control` annotations, disabled by default.

### `transcoder/json_helpers.py`

JSON frame helpers with compact separators for SSE frame construction.

## Transcoding Phases (Implementation History)

| Phase | Scope | Description |
|-------|-------|-------------|
| 1 | Foundation | Data model, config, helpers (no runtime change) |
| 2 | Body translation | Text-only non-streaming request/response |
| 3 | Streaming translation | SSE stream translation in both directions |
| 4 | Routing eligibility | Widens candidate set to transcodable accounts |
| 5 | Operator controls | Config docs, stats, dashboard cards |
| 6.1 | Tool-use | Bidirectional tool calling translation |
| 7 | Budget resolution | Effort-to-budget translation |
| 8 | Response-field compat | Configurable OpenAI reasoning field names |
| 9 | Streaming hot-path | Shared decoder, frame fan-out, synchronous translation |
| 10 | JSON backend | `eggpool.jsonx` abstraction (orjson/stdlib) |

## Loss Warning Kinds

Protected cache-control loss kinds (can trigger `TranscodeLossError` in reject mode):
- `cache_control_unsupported_by_target_protocol`
- `cache_control_feature_disabled`
- `cache_control_invalid_shape`
- `provider_extension_not_preserved`
- `stable_prefix_reordered_canonically`

Additional loss-warning kinds (informational):
- `tool_call_id_translated`, `tool_call_id_changed`
- `parallel_tool_calls_collapsed`
- `malformed_tool_arguments`, `invalid_tool_choice`
- `unsupported_tool_type`, `empty_tool_use_block`
- `tool_result_image_dropped`, `tool_result_error_passthrough`
- `pause_turn`, `non_text_content_dropped`, `tool_result_inferred`

## Thinking/Reasoning Transcoding

Phase 7+ adds thinking/reasoning support:
- `reasoning_effort` (OpenAI) → `thinking.budget_tokens` (Anthropic)
- `reasoning_content` history → thinking blocks
- Streaming thinking deltas with ordering preservation
- Capability-aware: supported/unsupported/unknown provider filtering
- Per-provider budget recompute at dispatch time

## Key Invariants

- Same-protocol requests pass through unchanged
- Streaming transcoder is synchronous (no async per-chunk)
- `CacheBoundaryTracker` is observational — never affects routing
- `QuotaFairScorer` never consumes cache/compression fields
- Loss policy defaults to `warn` (request proceeds, loss recorded)
- `select_transcoder()` is the single dispatch source of truth
- Frame helpers use compact JSON separators `(",", ":")` for wire efficiency
