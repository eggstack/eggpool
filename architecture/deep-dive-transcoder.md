# Deep Dive: Protocol Transcoding

Back to [Overview](overview.md)

## Purpose

Transparently translates request/response bodies between the public OpenAI
Chat, OpenAI Responses, and Anthropic Messages surfaces and the selected
provider wire surface. Native Gemini Interactions and `generateContent` are
also represented as concrete codecs. All alternate targets use a small
canonical semantic boundary.

## Architecture

```
Client surface        Canonical boundary       Upstream surface
──────────────        ──────────────────       ────────────────
OpenAI Chat    ───►   request intent      ───►  Chat / Responses / Messages
OpenAI Responses──►   response + events   ───►  Chat / Messages / Gemini
Anthropic Msg  ───►   source-owned IR     ───►  Chat / Responses
Native surface ───►   byte path or codec   ───►  same concrete surface
```

## Key Modules

### `transcoder/protocol.py`

`select_transcoder()` — single source of truth for dispatch. Returns the appropriate `BodyTranscoder` based on client vs upstream protocol.

### `transcoder/openai_to_anthropic.py`

`OpenAIToAnthropic` — body transcoder (OpenAI → Anthropic):
- `encode_request()`: translates OpenAI request body to Anthropic format
- accepts the provider-bound request as a read-only `Mapping` and returns a fresh target graph
- `decode_response()`: translates Anthropic response back to OpenAI
- Handles tools, tool_choice, parallel_tool_calls
- Uses verified target capabilities for native structured output, strict tools,
  and parallel-tool disabling; unknown compatible providers receive no native
  extension.
- Handles reasoning_effort → thinking budget translation
- Records cache boundary annotations and loss warnings

### `transcoder/anthropic_to_openai.py`

`AnthropicToOpenAI` — body transcoder (Anthropic → OpenAI):
- `encode_request()`: translates Anthropic request body to OpenAI format
- accepts the provider-bound request as a read-only `Mapping` and returns a fresh target graph
- `decode_response()`: translates OpenAI response back to Anthropic
- Drops unsupported `cache_control` annotations
- Maps thinking blocks to reasoning_content
- Maps Anthropic `output_config.format`, strict tools, and
  `disable_parallel_tool_use` to OpenAI fields only when the target capability
  explicitly permits it.

### `transcoder/streaming.py`

Streaming transcoder implementations for both directions:
- `OpenAIToAnthropicStreaming`
- `AnthropicToOpenAIStreaming`

Phase 9 optimization: one shared bounded decoder, synchronous
`translate_frame()`/`finish()` (no per-chunk `await`), compact JSON separators
`(",", ":")`, and coalesced output chunks.

### `transcoder/policy.py`

`TranscoderPolicy` — configuration and per-request state:
- Feature flags for optional semantic extensions; tool translation is baseline
  compatibility and the legacy `tools` field is retained as a no-op.
- Reasoning field names for OpenAI Chat Completions-compatible responses
- Loss policy (warn/reject)
- Budget resolution settings

### `transcoder/context.py`

`TranscodeContext` — per-request transcoding state dataclass carrying loss warnings, cache boundary tracker, tool-call ID map, and upstream protocol.

It also carries `client_surface`, the optional selected `WireProfile`, the
canonical request, and `ReasoningIntent`. Historical `client_protocol` and
`upstream_protocol` remain compatibility-family metadata while concrete wire
surface negotiation is staged. The canonical request is captured from the
original client payload and is never replaced by a previously translated
provider payload.

### `wire/ir.py` and `wire/codecs/`

The IR is intentionally small: typed messages/content blocks, portable
function tools and choices, normalized usage, response blocks, and bounded
streaming events. `ReasoningIntent` distinguishes unspecified, disabled,
effort, fixed-budget, adaptive, and toggle semantics. An effort label remains
an effort label until the selected provider/model capability supplies a
verified target encoding; codecs must not invent a budget.

`wire/codecs/base.py` defines the operations concrete codecs need: request
encoding, response/event decoding, and client response/event encoding.
`wire/codecs/compat.py` supplies executable Chat and Messages adapters;
`wire/codecs/defaults.py` supplies Responses, Gemini Interactions, and Gemini
`generateContent`. `wire/codecs/runtime.py` composes these codecs into the
selected-profile request/response/stream bridge. Same-surface native traffic
can still use the low-copy byte path when no semantic adaptation is needed.

### `transcoder/static_headers.py`

Protocol-required static headers for cross-protocol transcoding (e.g. `anthropic-version` for Anthropic upstreams).

### `transcoder/prepared.py`

`PreparedTranscode` — request-local preflight generation with mutable
diagnostics. It retains the translated payload and encoded body without a
recursive physical freeze; unchanged provider reuse adopts the payload through
`ProviderBoundRequest` and sends the existing bytes, while later provider
changes create a new provider-owned generation. Coordinator recompute uses the
same direct adoption boundary and does not make a defensive source deepcopy.

When the request payload contains provider-sensitive multimodal content
(images, documents, audio, or media inside tool results), the cached
`PreparedTranscode` cannot be safely reused across providers with different
multimodal capabilities. Plan 141 made this guarantee strict by moving the
definitive cross-protocol translation to **after** `SelectedAttempt` exists:
the `proxy_request` handler still runs a preflight translation for context
limit checks and loss policy validation, but it does **not** create a
`PreparedTranscode` for media-bearing requests so the coordinator can rebuild
the translated generation against the *selected* provider's capability row
using `catalog.cache.get_model_for_provider(model_id, selected.provider_id)`.
A retry that selects a different provider reverts the `ProviderBoundRequest`
to the original client payload before retranslating so provider A's
translation is never stacked on provider B's. Text-only and tool-only
requests continue to benefit from the prepared-transcode fast path.

### `transcoder/sensitive_media.py`

`request_has_provider_sensitive_media()` inspects a parsed request payload
for image, document, audio, and tool-result media forms. The coordinator
uses it as the validity gate for preflight reuse; text-only and tool-only
requests continue to benefit from the prepared-transcode fast path.

### `catalog/capabilities.py` — MultimodalCapabilities

Granular per-model media support, replacing coarse `supports_vision` for
transcoding decisions. `MediaCapability` indicates supported source forms
(`base64`, `url`) and optional `max_source_bytes`. `MultimodalCapabilities`
groups `image_input`, `document_input`, `audio_input`, `non_text_tool_result`,
and `max_serialized_request_bytes`. Capabilities are provider/model/protocol
scoped; unknown remains unknown and must never authorize a native field.

The bundled local-provider templates declare **endpoint-level** source-form
support against the *current* OpenAI-compatible surface contract only;
the actual multimodal capability of any individual loaded model/mmproj is
discovered at runtime and is not guaranteed by the template. Plan 142
confirmed these against the upstream docs:

- **Ollama OpenAI-compatible** (`docs.ollama.com/api/openai-compatibility`):
  declares `Base64 encoded image` as supported and `Image URL` as not
  supported. The native `/api/chat` surface accepts file paths and
  URLs in addition to base64, but the OpenAI-compatible surface does
  not; templates therefore declare `image_input.url = false`.
- **llama.cpp `llama-server`** docs accept remote URLs, base64, and
  local file paths for `image_url.url`; templates declare
  `image_input.url = true`.
- **vLLM OpenAI-compatible online serving** accepts URL images subject
  to `--allowed-media-domains`; templates declare `image_input.url = true`.

Source forms are capability-gated: the transcoder consults `MediaCapability`
flags before translating images and documents, emitting `unsupported_source_form`
loss warnings when the target provider cannot represent the source form.
Tool-result media is preserved when the target supports `non_text_tool_result`;
otherwise it is flattened to text with a `media_tool_result_flattened` warning.

When the configured cross-protocol `loss_policy = "reject"` fires (typically
for a selected provider that cannot represent a protected cache-control
field), the transcoder raises `TranscodeLossError`. The coordinator's
attempt-loop seam treats this as a client-validation outcome:

- `_finalize_selected_transcode_loss_rejection` converges the selected
  durable/runtime ownership synchronously through the canonical finalization
  owner (`CLIENT_ERROR / 400`, fail-closed on `DatabaseError`).
- The typed exception is re-raised so `proxy_request.py` renders it as
  HTTP 400 (`endpoint.error_response(400, "invalid_request_error")`).
- No retry selects another account, no upstream HTTP request is built/sent,
  and no provider health, suppression, quarantine, circuit, or durable
  backoff effect is applied.

Serialized via `model_capabilities_to_dict` / `dict_to_model_capabilities`
for the catalog cache round-trip. The canonical serialized contract contains
independent `toggle`, `effort`, and `budget` states; old `mode` values are
decoded as compatibility input only. Merge via `merge_model_capabilities`
applies known control dimensions independently, so an explicit unsupported
dimension clears stale accepted values and bounds.

### Media validation memory contract

Image and PDF data-URI paths retain the original encoded string for translated
output. They use encoded-length arithmetic to reject obvious oversize inputs
before strict base64 decoding; when strict decoding is required, its temporary
decoded buffer is released after validation and before the translated output
container is constructed. URL-source behavior and provider media limits are
unchanged.

### `transcoder/budget_resolver.py`

`resolve_thinking_budget()` — single source of truth for effort-to-budget translation:
1. Explicit `thinking.budget_tokens` (Anthropic style)
2. `reasoning_effort` via an explicit provider/model effort-to-budget mapping
3. `[transcoder.thinking_budget_defaults]`
4. Hard-coded fallback (low=1024, medium=4096, high=16384)

The hard-coded fallback is a legacy translation-policy compatibility fallback,
not discovered provider metadata.
`none` remains an effort intent during routing; it emits no budget and is only
mapped to an explicit target disable when that target contract verifies the
mapping. It is never silently rewritten as a binary toggle. Other effort labels, including current provider values such as `xhigh` or
`max`, require an explicit verified capability mapping. With no mapping,
`strict` rejects locally; `lenient` omits the target control and emits a
bounded `unknown_effort` warning rather than guessing a budget. Budgets are
clamped to `budget_tokens_min`/`budget_tokens_max`.

### `transcoder/ids.py`

`ToolCallIdMap` — per-request tool-call ID namespace mapping. Mints `call_<24 hex>` and `toolu_<24 hex>` IDs so the two namespaces never collide.

### `transcoder/usage.py`

Usage canonicalization across protocols (input_tokens ↔ prompt_tokens, cache counters, etc.).

### `transcoder/errors.py`

`TranscodeLossError` — raised when `loss_policy = "reject"` and protected loss kinds are detected. Protected kinds include both cache-control boundary losses (`CACHE_CONTROL_LOSS_KINDS`) and multimodal boundary losses (`MULTIMODAL_LOSS_KINDS`): `unsupported_modality`, `unsupported_source_form`, `media_tool_result_flattened`, and `document_media_type_unsupported`.

### `transcoder/cache_stability.py`

`CacheBoundaryTracker` — records what the transcoder did to `cache_control` annotations during translation. Append-only, bounded (64 annotations/request).

### Native prompt-cache translation

`TranscodingCapabilities.prompt_cache_breakpoints` is a provider/model
contract map, not a protocol-family switch. Each target entry declares a
`first_party` or verified `compatible_extension` dialect, supported TTL labels,
and a bounded breakpoint limit. OpenAI explicit content breakpoints map to
corresponding Anthropic cacheable blocks; Anthropic message/system block
controls map to corresponding OpenAI content parts only when that selected
contract exists. Source-only breakpoint markers are consumed and do not remain
on the target wire. The mapping emits structured loss metadata for overflow,
unsupported placement, TTL mismatch, and unrepresentable cache keys.
Tool-definition boundaries are never moved to a message boundary. An absent
OpenAI breakpoint is ordinary content and produces no warning or tracker
annotation; malformed, unsupported, and overflowed boundaries return as
unmapped, so explicit mode is emitted only when a target breakpoint was
actually written. No cache key is synthesized or persisted.

TTL labels are provider-specific and are never silently converted. OpenAI
automatic caching and `prompt_cache_key` grouping are not source intent for
Anthropic explicit cache boundaries. Native boundaries are preserved only when
the selected target contract supports the mapping; EggPool does not synthesize
additional cache controls.

### `transcoder/json_helpers.py`

General-purpose JSON helpers for loose-typed protocol payloads (`as_object`, `iter_objects`, `extract_text_blocks`, `decode_base64_payload`, base64 size arithmetic). Compact SSE separators live in `jsonx.dumps_bytes`.

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
| 11 | Content IR | Narrow content-block representation, `MultimodalCapabilities` (removed in Plan 140) |
| 134 | Multimodal transcode | Capability-aware source form gating, tool-result media preservation, multimodal loss-policy enforcement, serialized request-size validation |
| 140 | Local + multimodal closure | Corrected Ollama discovery, selected-provider capability resolution, provider-sensitive preflight reuse guard, canonical 413 lifecycle, audited local capability metadata, content IR removed |
| 141 | Final corrective closure | Post-selection provider-sensitive translation, 413 renderer, oversize finalization as proof-of-convergence, Responses deferral rationale, corrected vLLM image URL capability |
| 142 | Typed media rejections | Typed `CapabilityError` / `TranscodeLossError` at the attempt-loop seam, fail-closed durable finalization for selected-rejection paths, provider-bound 413, provider metadata URL-image facts |
| 144 | Final Responses closure | Established stateless Responses admission and terminal event semantics; superseded by the concrete multi-surface codecs in Plan 152 |

## Loss Warning Kinds

Protected cache-control loss kinds (can trigger `TranscodeLossError` in reject mode):
- `cache_control_unsupported_by_target_protocol`
- `cache_control_feature_disabled`
- `cache_control_invalid_shape`
- `provider_extension_not_preserved`
- `stable_prefix_reordered_canonically`
- `cache_breakpoint_unsupported_target`
- `cache_breakpoint_limit_exceeded`
- `cache_breakpoint_invalid_shape`
- `cache_ttl_mismatch`
- `cache_key_unrepresentable`

Protected multimodal loss kinds (can trigger `TranscodeLossError` in reject mode):
- `unsupported_modality` — entire modality not representable by target
- `unsupported_source_form` — specific source form (base64/url) not supported
- `media_tool_result_flattened` — tool-result media flattened to text
- `document_media_type_unsupported` — document media type not supported

Additional loss-warning kinds (informational):
- `tool_call_id_translated`, `tool_call_id_changed`
- `parallel_tool_calls_collapsed`
- `malformed_tool_arguments`, `invalid_tool_choice`
- `unsupported_tool_type`, `empty_tool_use_block`
- `tool_result_error_passthrough`
- `pause_turn`, `non_text_content_dropped`, `tool_result_inferred`
- `image_unsupported_format`, `image_too_large`, `pdf_too_large`
- `document_url_dropped`

## Thinking/Reasoning Transcoding

`ModelCapabilities.transcoding` is the narrow static/provider/model override
surface for native controls. It contains explicit protocol lists for
`native_structured_outputs`, `strict_tools`, and `parallel_tool_control`, plus
per-protocol `reasoning_efforts`. Missing values mean unknown support; no
native field is emitted merely because the protocol family matches.

OpenAI `reasoning_effort` is mapped to Anthropic `thinking.budget_tokens` only
through the existing target thinking capability budget mapping or the legacy
low/medium/high compatibility defaults. The catalog contract keeps effort
support independent from budget support. OpenAI's effort values are
model-dependent; `none` remains an effort value and is never converted into a
positive Anthropic budget or an invented toggle. Unmapped values are rejected or dropped according
to policy, never assigned a guessed medium budget. Anthropic manual thinking
budgets are not converted into fabricated OpenAI effort
values; only an explicit Anthropic effort value with a verified OpenAI target
mapping can cross that boundary.

Provider-bound thinking controls are normalized after selection by
`adapt_thinking_controls()`. Top-level `thinking_budget` and nested
`thinking.budget_tokens` follow the same `reject` / `warn_drop` /
`map_if_known` policy: an effort-only contract maps only one exact accepted
inverse entry, rejects unknown or ambiguous values, and never chooses by
dictionary order. The `none` contract has no implicit mapping target.
Historical reasoning content remains untouched.

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
- `QuotaFairScorer` never consumes cache fields
- Loss policy defaults to `warn` (request proceeds, loss recorded)
- `select_transcoder()` is the single dispatch source of truth
- Frame helpers use compact JSON separators `(",", ":")` for wire efficiency
