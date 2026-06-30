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
| `messages[tool]` | — | Dropped, warning emitted |
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
| `tools` | — | Dropped, warning emitted |
| `tool_choice` | — | Dropped, warning emitted |
| `messages[].content[non-text]` | — | Non-text blocks dropped, warning emitted |

### Response Bodies

#### Anthropic → OpenAI

| Anthropic field | OpenAI field | Notes |
|---|---|---|
| `id` | `id` | Prefixed `chatcmpl-` if needed |
| `model` | `model` | Passed through |
| `content[].text` | `message.content` | Text blocks concatenated |
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
| `pause_turn` | `tool_calls` | Lossy (semantics differ) |
| `model_context_window_exceeded` | `length` | Lossy (cause obscured) |

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

When `prefer_native = true` (the default), the router prefers accounts whose upstream protocol matches the client protocol. Transcoding only fires when no native account is available or when a higher-priority transcodable account is selected by routing rules.

## Operator Checklist

Before enabling transcoding in production:

1. **Review your client mix.** Are your clients actually mixed-protocol? If every client speaks OpenAI, transcoding does nothing.

2. **Set `loss_policy = "warn"` first.** Run in warn mode for at least a week to see what fields are being dropped. Check logs for `transcode.loss_warnings` entries.

3. **Audit loss warnings.** If a dropped field is critical for your use case (e.g., `top_k` for Anthropic-specific tuning), either:
   - Switch to a native-protocol provider for that model, or
   - Accept the loss and document it for your users.

4. **Test streaming.** Streaming transcoding is the most complex path. Verify with a real streaming client that:
   - Text content arrives correctly.
   - `finish_reason` / `stop_reason` is correct.
   - Usage values appear in the final chunk.

5. **Check your dashboard.** The Runtime page shows a Transcoding card with total transcoded requests, direction breakdown, and top loss warnings. Monitor this after enabling.

6. **Verify `prefer_native`.** With `prefer_native = true`, native-protocol accounts always win during routing. This is usually what you want. Set to `false` only if you need routing_priority to override protocol affinity.

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

Additional context fields that may appear:

| Field | Present when |
|---|---|
| `reason` | Explains why (e.g., `anthropic_unsupported`, `openai_unsupported`, `empty_messages`) |
| `from` / `to` | Shows original and mapped value for `value_clamped` and `lossy_mapping` |
| `default` | Shows the synthetic default for `missing_field` |

### Known Lossy Mappings

| Scenario | Kind | Detail |
|---|---|---|
| `temperature > 1.0` → Anthropic | `value_clamped` | Clamped to 1.0 |
| `max_tokens` missing from OpenAI request → Anthropic | `missing_field` | Defaulted to 4096 |
| `stop_sequence` → OpenAI `stop` | `lossy_mapping` | Sequence identity lost |
| `pause_turn` → OpenAI `tool_calls` | `lossy_mapping` | Semantic change |
| `model_context_window_exceeded` → OpenAI `length` | `lossy_mapping` | Cause obscured |

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

1. **Text-only in v1.** Tool calls, function calling, vision/image content, extended thinking, and structured outputs (`response_format` with `json_schema`) are not translated. These features are silently dropped with warnings. Full support is planned for later phases.

2. **Headers preserved verbatim.** Upstream response headers are passed through to the client without translation. Some Anthropic-specific headers (e.g., `anthropic-ratelimit-*`) may leak to OpenAI clients. This is cosmetic and harmless.

3. **All-or-nothing per deployment.** When `enabled = true`, transcoding applies to every account. Per-account opt-out is not supported in v1.

4. **No partial translation.** If a request contains both translatable and non-translatable features (e.g., text + tool calls), the entire request is translated. Non-translatable request parts are dropped with warnings — the transcoder refuses the request only when `loss_policy = "reject"`.

5. **`loss_policy = "reject"` is opt-in and strict for requests.** Any single request translation loss warning causes a 400 response before upstream dispatch. This can be surprising — use warn mode first to audit.

6. **Anthropic error types are best-effort.** The error type mapping covers the common cases but not every edge case. Unrecognised error types map to `api_error` (Anthropic) or `invalid_request_error` (OpenAI).

7. **Usage values are upstream-authoritative.** If the upstream reports unusual usage (e.g., negative cache tokens), the transcoder passes them through. There is no sanitisation.

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
