# Protocol Transcoding

EggPool transparently translates between OpenAI Chat Completions and Anthropic Messages protocols, letting clients speak one protocol while the upstream provider speaks the other.

## Overview

Protocol transcoding exists because the AI ecosystem has settled on two incompatible wire formats — OpenAI's Chat Completions API and Anthropic's Messages API. Most providers serve only one. Without transcoding, an OpenAI client can only reach OpenAI-compatible providers, and an Anthropic client can only reach Anthropic-compatible providers, even when the underlying model is the same.

**Transcoding is on by default.** Every EggPool data plane normalises the client request to the appropriate upstream wire format automatically: an OpenAI client posting to `/v1/chat/completions` reaches Anthropic upstreams (and vice versa) without any operator configuration. This is the primary behaviour of the router — clients should always see their expected protocol, regardless of the upstream provider's protocol.

The transcoder sits in the request path and:

1. **Rewrites the request body** before dispatch (OpenAI fields → Anthropic fields or vice versa).
2. **Decodes the response body** back to the client's expected format.
3. **Re-renders non-retryable errors** in the client protocol so error handling stays uniform.
4. **Translates streaming SSE events** chunk-by-chunk in real time.

Usage and cost fields are preserved exactly as the upstream reported them — no translation, no rounding, no loss.

## Configuration

The `[transcoder]` block is optional and rarely needs to be touched:

```toml
[transcoder]
# Optional loss policy: "warn" (default) or "reject"
loss_policy = "warn"
# Optional routing preference: native-protocol accounts outrank transcodable ones
prefer_native = true
```

At boot you'll see:

```
INFO  Protocol transcoding ENABLED (default) — clients may reach upstream accounts whose provider.protocols does not match the client protocol. loss_policy=warn prefer_native=true
```

### Deprecated escape hatch: `enabled = false`

The `enabled` flag is **deprecated** but still honoured as an escape hatch for operators who need to disable translation (for example, while diagnosing routing issues or pinning legacy behaviour):

```toml
[transcoder]
enabled = false   # DEPRECATED: forces legacy protocol-exact routing
```

When set, EggPool boots with a `WARNING` and reverts to the pre-default behaviour: every request must match its upstream protocol exactly. Cross-protocol requests will fail with `HTTP 400 ProtocolMismatchError`. This option will be removed in a future release.

## Translation Tables

### Request Bodies

#### OpenAI → Anthropic

| OpenAI field | Anthropic field | Notes |
|---|---|---|
| `model` | `model` | Passed through verbatim |
| `messages[system]` | `system` | Extracted from messages, concatenated |
| `messages[user]` | `messages[user]` | Text content passed through |
| `messages[assistant]` | `messages[assistant]` | Text content passed through |
| `temperature` | `temperature` | Clamped to ≤ 1.0 (Anthropic max) |
| `max_tokens` | `max_tokens` | Defaults to 4096 if missing |
| `stop` | `stop_sequences` | String → single-element list |
| `top_p` | — | Dropped, warning emitted |
| `frequency_penalty` | — | Dropped, warning emitted |
| `presence_penalty` | — | Dropped, warning emitted |
| `n` | — | Dropped when > 1, warning emitted |
| `logprobs` | — | Dropped, warning emitted |
| `top_logprobs` | — | Dropped, warning emitted |
| `response_format` | — | Dropped (json_schema mode), warning emitted |
| `seed` | — | Dropped, warning emitted |
| `user` | — | Dropped, warning emitted |
| `logit_bias` | — | Dropped, warning emitted |
| `tools` (function-shape) | `tools` (Anthropic-shape) | Translated field-by-field (see Tool-Use Transcoding below) |
| `tool_choice` | `tool_choice` | Translated between string and object shapes |
| `parallel_tool_calls: true` | omitted | Anthropic defaults to allowing parallel calls |
| `parallel_tool_calls: false` | dropped | Warning emitted; Anthropic has no parallel-disable knob |
| `tools[].function.strict` | — | Dropped, warning emitted (no Anthropic equivalent) |
| `stream_options` | — | Wrapper dropped with warning; `include_usage` lifted onto `TranscodeContext.request_include_usage` |
| `messages[assistant].tool_calls[]` | `messages[assistant].content[].tool_use` | Translated; id map mints new upstream ids |
| `messages[tool]` | `messages[user].content[].tool_result` | Translated; `tool_call_id` ↔ `tool_use_id` via id map |
| `messages[].content[non-text]` | — | Non-text blocks dropped, warning emitted |

#### Anthropic → OpenAI

| Anthropic field | OpenAI field | Notes |
|---|---|---|
| `model` | `model` | Passed through verbatim |
| `system` | `messages[system]` | Converted to system message |
| `messages[user]` | `messages[user]` | Text content passed through |
| `messages[assistant]` | `messages[assistant]` | Text content passed through |
| `temperature` | `temperature` | Passed through |
| `max_tokens` | `max_tokens` | Passed through |
| `stop_sequences` | `stop` | Single → string, multi → list |
| `metadata.user_id` | `user` | Mapped to OpenAI `user` field |
| `top_k` | — | Dropped, warning emitted |
| `thinking` | — | Dropped, warning emitted |
| `tools` (Anthropic-shape) | `tools` (function-shape) | Translated field-by-field (see Tool-Use Transcoding below) |
| `tool_choice` | `tool_choice` | Translated between object and string shapes |
| `tools[].cache_control` | — | Dropped, warning emitted; OpenAI auto-caches without explicit hints |
| `messages[assistant].content[].tool_use` | `messages[assistant].tool_calls[]` | Translated; id map mints new client ids |
| `messages[user].content[].tool_result` | `messages[tool]` | Translated; `tool_use_id` ↔ `tool_call_id` via id map |
| `messages[].content[non-text]` | — | Non-text blocks dropped, warning emitted |

### Response Bodies

#### Anthropic → OpenAI

| Anthropic field | OpenAI field | Notes |
|---|---|---|
| `id` | `id` | Prefixed `chatcmpl-` if needed |
| `model` | `model` | Passed through |
| `content[].text` | `message.content` | Text blocks concatenated |
| `content[].tool_use` | `message.tool_calls[]` | Translated; id map mints new client ids |
| `stop_reason` | `choices[].finish_reason` | Mapped (see table below) |
| `usage.input_tokens` | `usage.prompt_tokens` | Direct mapping |
| `usage.output_tokens` | `usage.completion_tokens` | Direct mapping |
| — | `usage.total_tokens` | Computed (prompt + completion) |

#### OpenAI → Anthropic

| OpenAI field | Anthropic field | Notes |
|---|---|---|
| `id` | `id` | Prefixed `msg_` if needed |
| `model` | `model` | Passed through |
| `message.content` | `content[].text` | Wrapped in text block |
| `message.tool_calls[]` | `content[].tool_use` | Translated; id map mints new upstream ids |
| `choices[].finish_reason` | `stop_reason` | Mapped (see table below) |
| `usage.prompt_tokens` | `usage.input_tokens` | Direct mapping |
| `usage.completion_tokens` | `usage.output_tokens` | Direct mapping |
| `message.refusal` | `content[].text` + `stop_reason=refusal` | Content replaced |

### Stop/Finish Reason Mapping

| Anthropic `stop_reason` | OpenAI `finish_reason` | |
|---|---|---|
| `end_turn` | `stop` | Lossless |
| `stop_sequence` | `stop` | Lossy (sequence identity lost) |
| `max_tokens` | `length` | Lossless |
| `tool_use` | `tool_calls` | Lossless |
| `refusal` | `content_filter` | Lossless |
| `pause_turn` | `tool_calls` + sentinel | Lossy; see Tool-Use Transcoding |
| `model_context_window_exceeded` | `length` | Lossy (cause obscured) |

### Tool-Use Transcoding

Tool calling is translated between OpenAI Chat Completions and Anthropic Messages in both directions, for streaming and non-streaming requests. The OpenAI client's `tools` and `tool_choice` reach Anthropic-only upstreams (e.g. MiniMax International) intact, and tool calls emitted by an Anthropic upstream are reconstructed as OpenAI `tool_calls` on the response. Anthropic clients driving OpenAI-only upstreams see the same round trip in reverse.

#### Field translation

**OpenAI `tools` → Anthropic `tools`**:

| OpenAI input | Anthropic output | Notes |
|---|---|---|
| `tools[i].type == "function"` | `tools[i]` (Anthropic-shape) | Lifted; `function.name` → `name`, `function.description` → `description`, `function.parameters` → `input_schema` |
| `tools[i].type` other than `"function"` | dropped | Warning `unsupported_tool_type` |
| `tools[i].function.strict` | — | Dropped; no Anthropic equivalent |

**Anthropic `tools` → OpenAI `tools`**:

| Anthropic input | OpenAI output | Notes |
|---|---|---|
| `tools[i].name`, `description`, `input_schema` | `tools[i].type == "function"` with `function.{name, description, parameters}` | `parameters` is the lifted `input_schema` |
| `tools[i].cache_control` | — | Dropped with warning; OpenAI auto-caches without explicit hints |

**`tool_choice` translation**:

| OpenAI input | Anthropic output | Notes |
|---|---|---|
| `"none"` | `{"type": "none"}` | |
| `"auto"` | omitted | Anthropic default |
| `"required"` | `{"type": "any"}` | |
| `{"type": "function", "function": {"name": "X"}}` | `{"type": "tool", "name": "X"}` | |
| anything else | omitted | Warning `invalid_tool_choice` |

| Anthropic input | OpenAI output | Notes |
|---|---|---|
| `{"type": "auto"}` | `"auto"` | |
| `{"type": "any"}` | `"required"` | |
| `{"type": "tool", "name": "X"}` | `{"type": "function", "function": {"name": "X"}}` | |
| `{"type": "none"}` | `"none"` | |
| anything else | `"auto"` | Warning `invalid_tool_choice` |

**`parallel_tool_calls`**:

- `parallel_tool_calls: true` is omitted on the Anthropic side (Anthropic defaults to allowing parallel calls).
- `parallel_tool_calls: false` is dropped with a warning — Anthropic has no parallel-disable knob. Each model invocation always permits multiple tool calls.

#### Tool-call id translation

Tool-call ids are protocol-shaped and never reused across the wire. OpenAI clients expect `call_<…>` ids; Anthropic upstreams expect `toolu_<…>` ids. The transcoder maintains a per-request `ToolCallIdMap` keyed on the originating `TranscodeContext.id_map` so the two namespaces stay independent.

| Direction | Source id | Generated id |
|---|---|---|
| OpenAI → Anthropic (assistant `tool_calls`) | `call_…` (client) | `toolu_<24 hex>` (via `generate_anthropic_id`) |
| Anthropic → OpenAI (assistant `content[].tool_use`) | `toolu_…` (upstream) | `call_<24 hex>` (via `generate_openai_id`) |
| OpenAI → Anthropic (`role: "tool"` history) | `call_…` (client `tool_call_id`) | `toolu_…` carried over as the `tool_use_id` on the Anthropic `tool_result` block |
| Anthropic → OpenAI (`tool_result` history) | `toolu_…` (upstream `tool_use_id`) | `call_…` carried over as the `tool_call_id` on the OpenAI `tool` message |

Both `generate_openai_id` and `generate_anthropic_id` produce 24 hex characters after the prefix (`call_` / `toolu_`), matching what real OpenAI and Anthropic responses emit. The `id_map` is per-`TranscodeContext` (i.e. per-request), so concurrent requests cannot collide.

Whenever the map mints a new id, the transcoder appends a `tool_call_id_translated` loss warning so operators can audit id translation traffic.

#### Message history translation

| OpenAI input | Anthropic output | Notes |
|---|---|---|
| `messages[i].role == "assistant"`, `tool_calls[j].id` (`call_…`) | `messages[i].content[k].type == "tool_use"`, `id` (`toolu_…`) | id via `id_map.register(call_…, toolu_…)` |
| `messages[i].role == "assistant"`, `tool_calls[j].function.name`, `function.arguments` | `tool_use.name`, `tool_use.input` | `arguments` JSON-parsed into an object; on parse failure the raw string is preserved as `{"__raw_arguments__": "<string>"}` and a `malformed_tool_arguments` warning is emitted |
| `messages[i].role == "assistant"`, mixed text + `tool_calls` | `content: [{type: "text", ...}, {type: "tool_use", ...}, ...]` | Anthropic permits mixed text + tool_use in one assistant turn |
| `messages[i].role == "tool"`, `content: str`, `tool_call_id` | `messages[i].role == "user"`, `content: [{type: "tool_result", tool_use_id, content: <str>, is_error: <bool>}]` | `tool_use_id` resolved via `id_map.to_upstream(call_id)` |
| `messages[i].role == "tool"`, `content: list` (mixed text/image) | `tool_result` with joined text; image parts dropped | Warning `tool_result_image_dropped` (image-tool-result translation lands in phase 6.2) |
| `messages[i].role == "tool"`, `is_error: true` | `tool_result.is_error: true` | Forwarded verbatim |

| Anthropic input | OpenAI output | Notes |
|---|---|---|
| `messages[i].content[k].type == "tool_use"`, `id` (`toolu_…`), `name`, `input` | `messages[i].role == "assistant"`, `tool_calls[k']` with `id` (`call_…`), `type: "function"`, `function.name`, `function.arguments` | `input` JSON-stringified into `arguments`; on encode failure the raw value is preserved as `{"__raw_arguments__": "<string>"}` with `malformed_tool_arguments` warning |
| `messages[i].content[k].type == "tool_result"`, `tool_use_id`, `content: str` | `messages[i].role == "tool"`, `tool_call_id` (`call_…`), `content: <str>` | |
| `messages[i].content[k].type == "tool_result"`, `content: list` | `messages[i].role == "tool"`, `content: <joined text>` | Text parts joined with `\n`; non-text dropped with `non_text_content_dropped` |
| `messages[i].content[k].type == "tool_result"`, `is_error: true` | `messages[i].role == "tool"`, `content: <error text>` | Warning `tool_result_error_passthrough` — OpenAI has no `is_error` field |

#### Streaming translation

**Anthropic → OpenAI (upstream SSE → client SSE)**:

Each Anthropic `content_block_start` with `content_block.type == "tool_use"` opens an indexed slot in the streaming transcoder's per-request state, keyed by the Anthropic `index`. The transcoder mints a fresh `call_<…>` id via `id_map.generate_openai_id()` and emits a `tool_calls` delta carrying `{index, id, type: "function", function: {name, arguments: ""}}`. Each subsequent `content_block_delta` with `delta.type == "input_json_delta"` appends `partial_json` to the slot's argument buffer and emits a `tool_calls` delta with `{index, function: {arguments: <chunk>}}`. The `content_block_stop` finalises the slot but emits nothing extra — the terminal `finish_reason: "tool_calls"` chunk plus `[DONE]` arrive on `message_delta`.

| Upstream event | Emitted client delta |
|---|---|
| `content_block_start` (`type: tool_use`, `id: toolu_X`, `name: fn`, `input: {}`) | `delta.tool_calls[i] = {index, id: call_Y, type: "function", function: {name: fn, arguments: ""}}` |
| `content_block_delta` (`type: input_json_delta`, `partial_json: <chunk>`) | `delta.tool_calls[i] = {index, function: {arguments: <chunk>}}` |
| `content_block_stop` (`index: i`) | (nothing) |
| `message_delta` (`stop_reason: tool_use`) | `delta: {}, finish_reason: "tool_calls"`, then `[DONE]` |

If `flush()` is called before a slot received its `content_block_stop`, the streaming transcoder still emits a final `tool_calls` delta with the accumulated `arguments`. If the accumulated JSON fails to parse, a `malformed_tool_arguments` warning is appended and the raw string is delivered anyway so the client can attempt to use the partial value.

**OpenAI → Anthropic (upstream SSE → client SSE)**:

OpenAI `tool_calls[*]` deltas may arrive split across many chunks (often with the id and name on the first chunk and incremental `arguments` on subsequent chunks). The transcoder buffers each call keyed on the OpenAI `tool_calls[*].index` until `finish_reason: "tool_calls"` arrives on the terminal delta, then assembles the Anthropic `tool_use` blocks in insertion order:

```
event: content_block_start
data: {"index": K, "content_block": {"type": "tool_use", "id": "toolu_<…>", "name": <name>, "input": <parsed JSON>}}
event: content_block_stop
data: {"index": K}
```

The `message_delta` then carries `stop_reason: "tool_use"` and `message_stop` closes the stream. Each block is emitted at a fresh `index` (the position in the assembled list), so multi-tool-call OpenAI traffic round-trips to multiple Anthropic `tool_use` blocks at distinct indices.

Edge cases:

- If an OpenAI delta arrives with a new `index`, a new slot is allocated.
- If an existing slot receives a non-empty `id` mid-stream, a `tool_call_id_changed` warning is appended and the new id is re-registered; callers should not update the id mid-stream.
- `tool_calls[*].function.arguments` is accumulated verbatim and parsed at `finish_reason: "tool_calls"`. On parse failure, the raw string is wrapped in `{"__raw_arguments__": "<string>"}` and a `malformed_tool_arguments` warning is emitted.

#### `pause_turn` handling

Anthropic's `pause_turn` `stop_reason` signals that the model paused mid-turn (typically to wait for a long-running tool). Phase 6.1 surfaces this to OpenAI clients by mapping `stop_reason: pause_turn` to `finish_reason: "tool_calls"` and appending a synthetic sentinel entry to `message.tool_calls`:

```json
{
  "id": "call_pause_turn_<request_id>",
  "type": "function",
  "function": {
    "name": "__eggpool_pause_turn__",
    "arguments": "{}"
  }
}
```

OpenAI clients detect the sentinel by name (`__eggpool_pause_turn__`) and resume the turn with the same `tool_use_id` they received from the original Anthropic `content_block_start`. A phase 6.5 follow-up will refine this into a first-class surface on the streaming transcoder; for now the inline sentinel is emitted on both streaming and non-streaming paths.

A `pause_turn` loss warning is also appended to `TranscodeContext.loss_warnings` whenever the sentinel is synthesized, so operators can audit how often pause-and-resume flows happen against their upstreams.

#### `stream_options.include_usage` lifting

OpenAI's `stream_options.include_usage` flag has no Anthropic analogue, so the wrapper object is dropped with a `dropped_field` warning when transcoding to an Anthropic upstream. The single field inside (`include_usage: bool`) is significant for usage accounting, so the transcoder lifts it onto `TranscodeContext.request_include_usage` before the streaming transcoder runs. The streaming transcoder reads `request_include_usage` from the context to decide whether to forward upstream usage chunks to the OpenAI client. The reverse direction (Anthropic → OpenAI) does not need this — OpenAI clients receive usage only when `stream_options.include_usage` was set on the request, which it wasn't.

#### Loss-warning kinds (tool-use)

Phase 6.1 introduced the following new `kind` values on `TranscodeContext.loss_warnings`:

| `kind` | Emitted when | Example fields |
|---|---|---|
| `tool_call_id_translated` | The id map mints a fresh id on either side of the translation | `field`, `from`, `to` |
| `tool_call_id_changed` | A streaming delta re-supplies a non-empty `id` for an existing tool_call slot | `field`, `from`, `to` |
| `parallel_tool_calls_collapsed` | `parallel_tool_calls: false` is dropped (no Anthropic equivalent) | `field` |
| `malformed_tool_arguments` | `tool_calls[*].function.arguments` or `content[].tool_use.input` fails to JSON-parse | `id`, optional `reason` |
| `invalid_tool_choice` | A `tool_choice` value cannot be mapped to the target shape | `field`, optional `from` |
| `unsupported_tool_type` | A `tools[i].type` other than `"function"` is dropped | `field`, `from` |
| `empty_tool_use_block` | Anthropic `stop_reason: tool_use` produced zero tool_use blocks | `field` |
| `tool_result_image_dropped` | Image content inside a `tool_result` block was dropped | `field` |
| `tool_result_error_passthrough` | Anthropic `tool_result.is_error` was forwarded as OpenAI `tool` content + warning (no `is_error` field in OpenAI shape) | `field` |
| `cache_control_dropped` | Anthropic `tools[].cache_control` was dropped during Anthropic → OpenAI translation | `field` |
| `pause_turn` | `stop_reason: pause_turn` was mapped to `finish_reason: tool_calls` plus a sentinel tool_call | `field`, `to` |
| `non_text_content_dropped` | A non-text content part inside a translated message was dropped | `field` |
| `tool_result_inferred` | A `tool_use_id` was inferred from request context when the client did not supply one | `field`, optional `from` |

The complete catalogue lives in `eggpool.transcoder.LOSS_WARNING_KINDS`.

### Error Envelopes

| Protocol | Status code field | Error type field | Message field | Request ID field |
|---|---|---|---|---|
| OpenAI | HTTP status | `error.type` | `error.message` | `request_id` |
| Anthropic | HTTP status | `type` | `error.message` | `request_id` |

Error types are mapped bidirectionally:

| Anthropic error type | OpenAI error type |
|---|---|
| `invalid_request_error` | `invalid_request_error` |
| `authentication_error` | `invalid_api_key` |
| `permission_error` | `insufficient_quota` |
| `not_found_error` | `invalid_request_error` |
| `request_too_large` | `invalid_request_error` |
| `rate_limit_error` | `rate_limit_exceeded` |
| `api_error` | `api_error` |
| `overloaded_error` | `api_error` |
| `billing_error` | `insufficient_quota` |
| `timeout_error` | `timeout` |
| `conflict_error` | `invalid_request_error` |
| `internal_error` | `api_error` |

### Usage (Canonicalisation)

Both protocols are normalised to a common internal representation before reaching the cost calculator:

| Canonical field | OpenAI source | Anthropic source |
|---|---|---|
| `prompt_tokens` | `prompt_tokens` | `input_tokens` |
| `completion_tokens` | `completion_tokens` | `output_tokens` |
| `total_tokens` | `total_tokens` | Computed (input + output) |
| `cache_creation_tokens` | — | `cache_creation_input_tokens` |
| `cache_read_tokens` | — | `cache_read_input_tokens` |

Cache token fields are only populated for Anthropic upstreams.

For request finalization and dashboard accounting, streaming and non-streaming
responses use the same protocol-specific usage extractors. Malformed, negative,
or non-finite token counts are passed through the shared token-count coercion
path and treated as zero for internal cost/quota calculation. The downstream
response body is not rewritten solely for accounting normalization.

## Provider Notes

Transcoding is triggered when the client protocol differs from the selected account's upstream protocol. Providers that need transcoding:

| Provider | Upstream protocol | When transcoding fires |
|---|---|---|
| Anthropic | Anthropic | Client sends OpenAI requests to a model routed to an Anthropic account |
| OpenAI | OpenAI | Client sends Anthropic requests to a model routed to an OpenAI account |
| OpenRouter | OpenAI | Same as OpenAI — Anthropic clients hitting OpenRouter models |
| DeepSeek | OpenAI | Anthropic clients hitting DeepSeek models |
| Together AI | OpenAI | Anthropic clients hitting Together models |
| Google Gemini | OpenAI | Anthropic clients hitting Gemini models |

Providers that support both protocols natively (like OpenCode Go) may never trigger transcoding if accounts are configured with the right protocol flags.

When `prefer_native = true` (the default), the router uses native protocol as a tie-breaker inside the selected `routing_priority` tier. Transcoding still fires when no native account is available, when a transcodable account has a better quota score, or when a higher-priority provider tier is transcodable.

## Operator Checklist

For production deployments using transcoding:

1. **Review your client mix.** If every client and every selected upstream account already speak the same protocol, transcoding will stay idle even though it is enabled.

2. **Set `loss_policy = "warn"` first.** Run in warn mode for at least a week to see what fields are being dropped. Check logs for `transcode.loss_warnings` entries.

3. **Audit loss warnings.** If a dropped field is critical for your use case (e.g., `top_k` for Anthropic-specific tuning), either:
   - Switch to a native-protocol provider for that model, or
   - Accept the loss and document it for your users.

4. **Test streaming.** Streaming transcoding is the most complex path. Verify with a real streaming client that:
   - Text content arrives correctly.
   - `finish_reason` / `stop_reason` is correct.
   - Usage values appear in the final chunk.
   - Tool-call deltas arrive in insertion order and terminate with `finish_reason: "tool_calls"` plus `[DONE]`. For tool-using requests against Anthropic-only upstreams, ensure the synthetic `__eggpool_pause_turn__` sentinel (if any) is detected and surfaced to the application logic.

5. **Check your dashboard.** The Runtime page shows a Transcoding card with total transcoded requests, direction breakdown, and top loss warnings. Monitor this after adding cross-protocol providers or clients.

6. **Verify `prefer_native`.** With `prefer_native = true`, native-protocol accounts win score ties inside the selected priority tier. `routing_priority` still selects the provider tier before this tie-breaker runs. Set `prefer_native = false` only if quota score alone should decide same-tier native-versus-transcoded ordering.

7. **Set `loss_policy = "reject"` only if you've confirmed no critical request fields are dropped.** In reject mode, lossy request-body translation returns a 400 before dispatch. Loss warnings discovered while decoding upstream responses are still logged and shown in diagnostics.

## Loss Warning Reference

Every loss warning is a structured dict with at minimum `kind` and `field`. The possible `kind` values:

| `kind` | Meaning | Example |
|---|---|---|
| `dropped_field` | A field was removed because the target protocol has no equivalent | `{"kind": "dropped_field", "field": "top_p", "reason": "anthropic_unsupported"}` |
| `missing_field` | A required field was absent and a default was inserted | `{"kind": "missing_field", "field": "max_tokens", "default": 4096}` |
| `value_clamped` | A value was adjusted to fit the target protocol's range | `{"kind": "value_clamped", "field": "temperature", "from": 2.0, "to": 1.0}` |
| `lossy_mapping` | A value was mapped but semantics changed | `{"kind": "lossy_mapping", "field": "stop_reason", "from": "stop_sequence", "to": "stop"}` |
| `inserted_field` | An empty or missing structure was synthesised | `{"kind": "inserted_field", "field": "messages", "reason": "empty_messages"}` |
| `tool_call_id_translated` | The id map minted a new id on either side of the translation | `{"kind": "tool_call_id_translated", "field": "messages[assistant].tool_calls[].id", "from": "call_…", "to": "toolu_…"}` |
| `tool_call_id_changed` | A streaming delta re-supplied a non-empty `id` for an existing tool_call slot | `{"kind": "tool_call_id_changed", "field": "tool_calls[]", "from": "call_X", "to": "call_Y"}` |
| `parallel_tool_calls_collapsed` | `parallel_tool_calls: false` was dropped (no Anthropic equivalent) | `{"kind": "parallel_tool_calls_collapsed", "field": "parallel_tool_calls"}` |
| `malformed_tool_arguments` | Tool argument JSON failed to parse; raw string delivered as `__raw_arguments__` | `{"kind": "malformed_tool_arguments", "id": "call_…"}` |
| `invalid_tool_choice` | A `tool_choice` value could not be mapped to the target shape | `{"kind": "invalid_tool_choice", "field": "tool_choice"}` |
| `unsupported_tool_type` | A `tools[i].type` other than `"function"` was dropped | `{"kind": "unsupported_tool_type", "field": "tools[].type"}` |
| `empty_tool_use_block` | Anthropic `stop_reason: tool_use` produced zero tool_use blocks | `{"kind": "empty_tool_use_block", "field": "content[].tool_use"}` |
| `tool_result_image_dropped` | Image content inside a `tool_result` block was dropped (phase 6.2 will translate) | `{"kind": "tool_result_image_dropped", "field": "messages[tool].content"}` |
| `tool_result_error_passthrough` | Anthropic `tool_result.is_error` was forwarded as OpenAI `tool` content + warning | `{"kind": "tool_result_error_passthrough", "field": "tool_result.is_error"}` |
| `cache_control_dropped` | Anthropic `tools[].cache_control` was dropped during Anthropic → OpenAI translation | `{"kind": "cache_control_dropped", "field": "tools[].cache_control"}` |
| `pause_turn` | `stop_reason: pause_turn` was mapped to `finish_reason: tool_calls` plus a sentinel tool_call | `{"kind": "pause_turn", "field": "stop_reason", "to": "tool_calls"}` |
| `non_text_content_dropped` | A non-text content part inside a translated message was dropped | `{"kind": "non_text_content_dropped", "field": "messages[assistant].content"}` |
| `tool_result_inferred` | A `tool_use_id` was inferred from request context when the client did not supply one | `{"kind": "tool_result_inferred", "field": "tool_use_id"}` |

Additional context fields that may appear:

| Field | Present when |
|---|---|
| `reason` | Explains why (e.g., `anthropic_unsupported`, `openai_unsupported`, `empty_messages`) |
| `from` / `to` | Shows original and mapped value for `value_clamped`, `lossy_mapping`, `tool_call_id_translated`, `tool_call_id_changed`, `pause_turn` |
| `default` | Shows the synthetic default for `missing_field` |
| `id` | Tool-call id associated with the warning (e.g., `malformed_tool_arguments`) |

The complete catalogue lives in `eggpool.transcoder.LOSS_WARNING_KINDS`.

### Known Lossy Mappings

| Scenario | Kind | Detail |
|---|---|---|
| `temperature > 1.0` → Anthropic | `value_clamped` | Clamped to 1.0 |
| `max_tokens` missing from OpenAI request → Anthropic | `missing_field` | Defaulted to 4096 |
| `stop_sequence` → OpenAI `stop` | `lossy_mapping` | Sequence identity lost |
| `pause_turn` → OpenAI `tool_calls` + `__eggpool_pause_turn__` sentinel | `lossy_mapping` + `pause_turn` | Semantic change; see Tool-Use Transcoding |
| `model_context_window_exceeded` → OpenAI `length` | `lossy_mapping` | Cause obscured |
| `parallel_tool_calls: false` → Anthropic | `parallel_tool_calls_collapsed` | Anthropic has no parallel-disable knob |
| `tools[].function.strict` → Anthropic | `dropped_field` | No Anthropic equivalent |
| `tools[].cache_control` → OpenAI | `cache_control_dropped` | OpenAI auto-caches without explicit hints |
| `function_call` / `functions` (deprecated OpenAI API) | `dropped_field` | Deprecated; clients should migrate to `tools` |
| Tool-call id rewritten (`call_…` ↔ `toolu_…`) | `tool_call_id_translated` | Map is per-request, never collides across requests |

## Performance Characteristics

- **Body translation**: ~50µs per request. This is a pure Python dict transformation — no I/O, no network calls.
- **Streaming translation**: One state machine per request. The state machine processes SSE frames incrementally — no buffering of the full stream.
- **Memory**: The streaming transcoder holds a small frame buffer (max 64KB per incomplete SSE line) and a UTF-8 incremental decoder.
- **No additional network hops**: Transcoding happens inside the existing request path. There is no sidecar or proxy.

The overhead is negligible compared to upstream latency (typically 200ms–30s). You will not measure a difference in p99 latency from transcoding alone.

## Pricing Catalog Cache

The transcoder depends on the upstream pricing catalogs (OpenRouter, OpenCode Zen, ...) that the resolver pipeline caches per catalog. Each `TTLCache` is bounded by a configurable `max_entries` knob (default `4096`, LRU eviction on store). Unparsed `raw` payloads are stripped after the catalog is parsed so the cache footprint stays small even with hundreds of models in the upstream catalog.

```toml
[pricing.catalogs.openrouter]
enabled = true
priority = 100
ttl_seconds = 86400
max_entries = 4096  # bound the in-memory catalog cache; oldest entries evict first
```

Lower `max_entries` to trade catalog completeness for steady-state RSS on memory-constrained hosts (Raspberry Pi, SBC). The OpenRouter catalog ships ~250+ entries today, so the default has plenty of headroom.

## Known Limitations

1. **Tool calling translated; vision / thinking / structured outputs deferred.** As of phase 6.1, tool use and function calling are translated in both directions for streaming and non-streaming requests (see Tool-Use Transcoding). Vision / image content, extended thinking, structured outputs (`response_format` with `json_schema`), PDF / document input, and audio input are not yet translated; these features are dropped with warnings. They land in subsequent phase 6 sub-phases.

2. **Headers preserved verbatim.** Upstream response headers are passed through to the client without translation. Some Anthropic-specific headers (e.g., `anthropic-ratelimit-*`) may leak to OpenAI clients. This is cosmetic and harmless.

3. **All-or-nothing per deployment.** When `enabled = true`, transcoding applies to every account. Per-account opt-out is not supported in v1.

4. **No partial translation.** If a request contains both translatable and non-translatable features (e.g., text + vision content), the entire request is translated. Non-translatable request parts are dropped with warnings — the transcoder refuses the request only when `loss_policy = "reject"`.

5. **`loss_policy = "reject"` is opt-in and strict for requests.** Any single request translation loss warning causes a 400 response before upstream dispatch. This can be surprising — use warn mode first to audit.

6. **Anthropic error types are best-effort.** The error type mapping covers the common cases but not every edge case. Unrecognised error types map to `api_error` (Anthropic) or `invalid_request_error` (OpenAI).

7. **Usage values are upstream-authoritative.** If the upstream reports unusual usage (e.g., negative cache tokens), the transcoder passes them through. There is no sanitisation.

8. **Tool-call id remapping is opaque to clients.** Every cross-protocol tool call has its id rewritten (`call_…` ↔ `toolu_…`) by the `id_map`. Clients that compare ids across turns see the rewritten id, not the original. The mapping is per-request so concurrent requests never collide.

9. **`pause_turn` is surfaced as a sentinel tool call.** Anthropic's `pause_turn` stop_reason becomes `finish_reason: "tool_calls"` plus a synthetic `__eggpool_pause_turn__` tool_call entry. OpenAI clients detect the pause by name and resume with the same `tool_use_id`. The sentinel is a phase 6.1 placeholder; phase 6.5 will refine the surface.

## Troubleshooting

### Confirm transcoding is active

Check the boot log:

```
INFO  Protocol transcoding ENABLED (default) — clients may reach upstream accounts whose provider.protocols does not match the client protocol. loss_policy=warn prefer_native=true
```

If you see `WARNING  Protocol transcoding DISABLED`, the `[transcoder] enabled = false` escape hatch is in effect and cross-protocol requests will fail.

### Read per-request transcoding logs

Every transcoded request emits a structured INFO log:

```
INFO  request_id=req_abc123 client_protocol=openai upstream_protocol=anthropic
      account=anthropic-primary provider=anthropic native_match=false
      loss_warnings=2
```

Key fields:
- `client_protocol` / `upstream_protocol` — confirms translation is happening
- `native_match=false` — means the request was transcoded (true = no translation needed)
- `loss_warnings` — count of dropped fields / lossy mappings for this request

### Check loss warnings in logs

Search for `dropped_field`, `value_clamped`, or `lossy_mapping` in structured logs. Each entry includes the request ID and field name.

### Monitor via the dashboard

The Runtime page (`/runtime`) includes a Transcoding card showing:
- Total transcoded requests
- Direction breakdown (OpenAI→Anthropic vs Anthropic→OpenAI)
- Top loss warnings by kind and field

### Monitor via CLI

```bash
eggpool stats transcoding --period 7d
eggpool stats transcoding --period 30d --json
```

### Disable transcoding (deprecated escape hatch)

Set `enabled = false` in `[transcoder]` and restart. All requests will now require protocol match — a client speaking OpenAI to an Anthropic-only account will get a protocol mismatch error. This option is deprecated; transcoding will become unconditionally on in a future release.

### Debug a specific request

1. Find the `request_id` from the per-request log or dashboard.
2. Search logs for that request ID.
3. Look for `loss_warnings` — each entry tells you which field was dropped and why.
4. If the issue is a missing field, check the translation tables above to understand what the transcoder does and does not support.
