# Phase 2 — Body translation

## Goal

Implement the **text-only, non-streaming** body translators in both
directions and wire them into `RequestCoordinator.execute()` so that when
`transcode_required` is set, the coordinator:

1. Transcodes the client request body to the upstream protocol before
   dispatch.
2. Transcodes the upstream success response body back to the client
   protocol before returning.
3. Re-renders upstream non-retryable error envelopes (status ≥ 400 that
   classify as `BAD_REQUEST`) in the client protocol.

This is the first phase where users see real change. With
`[transcoder] enabled = true` and a transcodable routing target (phase 4),
a request from OpenCode to an Anthropic-only MiniMax International
upstream now succeeds.

## Scope

In scope:

- Text-only request body translation in both directions:
  OpenAI → Anthropic and Anthropic → OpenAI.
- Text-only non-streaming response body translation in both directions.
- Error envelope parsing (`upstream` → canonical) and re-rendering
  (`canonical` → `client`).
- Coordinator integration in `_execute_non_streaming` and the
  non-retryable error path.
- Loss-of-information tracking via `TranscodeContext.loss_warnings`.
- Unit tests for every translator against canonical fixtures.
- Integration test that exercises an OpenCode-shaped request against a
  mocked Anthropic upstream through the real coordinator.

Out of scope:

- Streaming SSE translation. (Phase 3.)
- Tool calls, vision, thinking, structured outputs. (Phase 6.)
- Routing widening. (Phase 4 — body translation is invoked regardless of
  how the upstream was selected; it works even when only same-protocol
  accounts exist, in which case it never fires.)

## Files to create

```
src/eggpool/transcoder/
├── protocol.py             # BodyTranscoder Protocol + factory + dispatch
├── openai_to_anthropic.py  # OpenAI request/response/error translator
└── anthropic_to_openai.py  # Anthropic request/response/error translator

tests/unit/test_transcoder/
├── test_openai_to_anthropic_body.py
├── test_anthropic_to_openai_body.py
├── test_openai_to_anthropic_response.py
├── test_anthropic_to_openai_response.py
├── test_error_translation.py
└── fixtures/
    ├── openai_text_request.json
    ├── openai_text_response.json
    ├── openai_error_response.json
    ├── anthropic_text_request.json
    ├── anthropic_text_response.json
    └── anthropic_error_response.json

tests/integration/
└── test_transcode_body.py
```

## Files to modify

```
src/eggpool/api/proxy_request.py        # pre-translate body before context construction
src/eggpool/request/coordinator.py      # call transcoder on upstream success + error
src/eggpool/transcoder/__init__.py      # public exports
```

## Detailed design

### 1. `BodyTranscoder` Protocol (`src/eggpool/transcoder/protocol.py`)

```python
from __future__ import annotations

from typing import Any, Protocol


class BodyTranscoder(Protocol):
    """Translates a request or response body between two protocols."""

    client_protocol: str
    upstream_protocol: str

    def encode_request(
        self,
        payload: dict[str, Any],
        context: TranscodeContext,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Translate a client request payload to the upstream protocol.

        Returns ``(translated_payload, loss_warnings)``. The translator
        must not mutate the input dict. The returned dict is fed to
        ``encode_json_body`` for outbound serialization.
        """

    def decode_response(
        self,
        payload: dict[str, Any],
        context: TranscodeContext,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Translate an upstream success response back to the client
        protocol. ``payload`` is the parsed JSON body of the upstream
        2xx response. The returned dict is serialized as the client
        response.
        """

    def reencode_error(
        self,
        upstream_status: int,
        upstream_payload: dict[str, Any] | None,
        context: TranscodeContext,
    ) -> tuple[int, dict[str, Any], list[dict[str, Any]]]:
        """Re-render an upstream non-retryable error in the client
        protocol. ``upstream_status`` is preserved (we do not invent
        status codes). ``upstream_payload`` may be None if the upstream
        returned an empty body.

        The returned dict is the JSON body that goes to the client.
        """


def select_transcoder(
    *,
    client_protocol: str,
    upstream_protocol: str,
) -> BodyTranscoder | None:
    """Return the body transcoder for a protocol pair, or None when the
    pair matches and no translation is needed."""
    if client_protocol == upstream_protocol:
        return None
    if client_protocol == "openai" and upstream_protocol == "anthropic":
        return OpenAIToAnthropic()
    if client_protocol == "anthropic" and upstream_protocol == "openai":
        return AnthropicToOpenAI()
    raise ConfigError(
        f"Unknown protocol pair for transcoding: "
        f"{client_protocol!r} → {upstream_protocol!r}"
    )
```

The factory raises `ConfigError` so a misconfigured `protocols` field in
phase 4 produces a clean 5xx rather than a silent fallback.

### 2. `OpenAIToAnthropic` (`src/eggpool/transcoder/openai_to_anthropic.py`)

Text-only v1 scope. Every other field is dropped with a warning, except
those listed below.

#### `encode_request(payload, context)`

| OpenAI input | Anthropic output | Notes |
|---|---|---|
| `model` | `model` | verbatim |
| `messages[*].role == "system"` | collected, joined with `\n\n`, set as top-level `system` | multiple system messages are joined |
| `messages[*].role == "user"` with `content: str` | `{role: "user", content: [{type: "text", text: content}]}` | |
| `messages[*].role == "user"` with `content: [...]` | role=`user`, content list mapped part-by-part | text parts pass through; everything else warns and is dropped |
| `messages[*].role == "assistant"` with `content: str` | `{role: "assistant", content: [{type: "text", text: content}]}` | |
| `messages[*].role == "tool"` | first message of pair is dropped with warning | tool translation is phase 6 |
| `temperature` | `temperature` | clamped to `[0.0, 1.0]` if > 1.0, with warning |
| `top_p` | `top_p` | passthrough |
| `max_tokens` | `max_tokens` | REQUIRED by Anthropic; if absent, default to 4096 with warning |
| `stream` | `stream` | passthrough; v1 only acts on `stream=false` |
| `stop` (string) | `stop_sequences: [stop]` | |
| `stop` (list) | `stop_sequences: stop` | capped at 4 with warning if longer |
| `n` | dropped with warning | Anthropic does not support multiple completions |
| `presence_penalty`, `frequency_penalty`, `logit_bias`, `logprobs`, `top_logprobs`, `user`, `seed`, `metadata`, `store`, `service_tier`, `parallel_tool_calls`, `stream_options`, `response_format`, `reasoning_effort` | dropped with warning | unsupported in v1 |
| `tools`, `tool_choice` | dropped with warning | phase 6 |
| `functions`, `function_call` | dropped with warning | deprecated; phase 6 if ever |

Warnings emitted via `context.loss_warnings.append({...})` with the shape:

```python
{
    "kind": "dropped_field",
    "field": "presence_penalty",
    "reason": "anthropic_unsupported",
}
```

or for value clamping:

```python
{
    "kind": "value_clamped",
    "field": "temperature",
    "from": 1.7,
    "to": 1.0,
}
```

#### `decode_response(payload, context)`

| Anthropic input | OpenAI output | Notes |
|---|---|---|
| `id` | `id` | verbatim |
| `model` | `model` | verbatim |
| `type: "message"`, `role: "assistant"` | dropped | OpenAI uses `choices[].message.role` |
| `content[*].type == "text"` | joined with empty separator, set as `choices[0].message.content` | multiple text blocks are concatenated |
| `content[*].type == "tool_use"` | dropped with warning | phase 6 |
| `content[*].type == "thinking"` | dropped with warning | phase 6 |
| `content[*].type == "redacted_thinking"` | dropped with warning | phase 6 |
| `stop_reason` | `choices[0].finish_reason` | see mapping table below |
| `usage.input_tokens` | `usage.prompt_tokens` | |
| `usage.output_tokens` | `usage.completion_tokens` | |
| `usage.cache_read_input_tokens` | `usage.prompt_tokens_details.cached_tokens` | if > 0 |
| `usage.cache_creation_input_tokens` | `usage.prompt_tokens_details.cache_creation_tokens` | v1 extension; OpenAI doesn't define this field but it is harmless |
| `usage` (other fields) | dropped | |
| `stop_sequence` | dropped | OpenAI doesn't distinguish |

Stop reason mapping:

| Anthropic | OpenAI |
|---|---|
| `end_turn` | `stop` |
| `max_tokens` | `length` |
| `stop_sequence` | `stop` (loss-of-info: client cannot distinguish; warn) |
| `tool_use` | `tool_calls` |
| `refusal` | `content_filter` |
| `pause_turn` | `tool_calls` (loss-of-info; warn; phase 6 will add a sentinel) |
| `model_context_window_exceeded` | `length` |

The OpenAI response is wrapped:

```json
{
    "id": "<anthropic-id>",
    "object": "chat.completion",
    "created": 0,
    "model": "<anthropic-model>",
    "choices": [{
        "index": 0,
        "message": {
            "role": "assistant",
            "content": "<concatenated text>",
            "refusal": null
        },
        "finish_reason": "<mapped>"
    }],
    "usage": {
        "prompt_tokens": <int>,
        "completion_tokens": <int>,
        "total_tokens": <int>,
        "prompt_tokens_details": {"cached_tokens": <int>, "cache_creation_tokens": <int>}
    }
}
```

`created` is set to the Unix epoch second received on the upstream
response if present, else 0. v1 emits 0 always to keep the translator
pure; phase 6 may thread wall-clock through `TranscodeContext`.

`system_fingerprint` is omitted (no Anthropic analogue).

#### `reencode_error(upstream_status, upstream_payload, context)`

Anthropic errors arrive as:

```json
{"type": "error", "error": {"type": "...", "message": "..."}}
```

Parse with a defensive helper from `transcoder.errors`. Map Anthropic
error types to OpenAI `type` values:

| Anthropic `error.type` | OpenAI `type` |
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
| (unknown) | `invalid_request_error` |

`code` is the upstream status code as a string. `param` is omitted (no
field-level info).

### 3. `AnthropicToOpenAI` (`src/eggpool/transcoder/anthropic_to_openai.py`)

Mirror of the above.

#### `encode_request(payload, context)`

| Anthropic input | OpenAI output | Notes |
|---|---|---|
| `model` | `model` | verbatim |
| `system: str` | prepend `{role: "system", content: system}` to messages | |
| `system: [{type: "text", text: ...}, ...]` | join text parts with `\n\n`, prepend as system message | |
| `messages[*]` with `content: str` | `{role, content: str}` | passthrough for both roles |
| `messages[*]` with `content: [{type: "text", text: ...}, ...]` | concatenated text as `content: str` | OpenAI v1 text-only |
| `messages[*].content[*]` of other types | dropped with warning | phase 6 |
| `max_tokens` | `max_tokens` | passthrough |
| `temperature` | `temperature` | passthrough (Anthropic max is 1.0; OpenAI allows 2.0; we forward verbatim) |
| `top_p` | `top_p` | passthrough |
| `top_k` | dropped with warning | OpenAI has no equivalent |
| `stop_sequences` | `stop` (if 1) or `stop` (list) | length-clamped to 4 |
| `metadata.user_id` | `user` | passthrough |
| `metadata` (other fields) | dropped | |
| `stream` | `stream` | v1 only acts on `stream=false` |
| `thinking` | dropped with warning | phase 6 |
| `context_management`, `container`, `mcp_servers`, `service_tier` | dropped with warning | phase 6 |
| `tools`, `tool_choice` | dropped with warning | phase 6 |

If the request has zero `messages`, prepend `{role: "user", content: ""}`
with warning (OpenAI requires ≥1 message).

#### `decode_response(payload, context)`

| OpenAI input | Anthropic output | Notes |
|---|---|---|
| `id` | `id` | verbatim |
| `model` | `model` | verbatim |
| `choices[0].message.content` | `content[*].type="text"`, `text=<content>` | single block |
| `choices[0].message.refusal` | `content[*].type="text"`, `text=<refusal>`, `stop_reason="refusal"` | refusal is text-only in v1 |
| `choices[0].finish_reason` | `stop_reason` | inverse of the OpenAI mapping above |
| `usage.prompt_tokens` | `usage.input_tokens` | |
| `usage.completion_tokens` | `usage.output_tokens` | |
| `usage.prompt_tokens_details.cached_tokens` | `usage.cache_read_input_tokens` | |
| `usage.prompt_tokens_details.cache_creation_tokens` | `usage.cache_creation_input_tokens` | v1 extension |
| `choices[].message.tool_calls` | dropped with warning | phase 6 |
| `system_fingerprint` | dropped | |

Anthropic response envelope:

```json
{
    "id": "<openai-id>",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "<content>"}],
    "model": "<openai-model>",
    "stop_reason": "<mapped>",
    "stop_sequence": null,
    "usage": {
        "input_tokens": <int>,
        "output_tokens": <int>,
        "cache_read_input_tokens": <int>,
        "cache_creation_input_tokens": <int>
    }
}
```

#### `reencode_error(upstream_status, upstream_payload, context)`

OpenAI errors arrive as:

```json
{"error": {"message": "...", "type": "...", "code": "...", "param": "..."}}
```

Map OpenAI `type` to Anthropic `type`:

| OpenAI `type` | Anthropic `type` |
|---|---|
| `invalid_request_error` | `invalid_request_error` |
| `invalid_api_key` | `authentication_error` |
| `insufficient_quota` | `billing_error` |
| `rate_limit_exceeded` | `rate_limit_error` |
| `api_error` | `api_error` |
| `timeout` | `timeout_error` |
| (unknown) | `api_error` |

### 4. Coordinator wiring (`src/eggpool/request/coordinator.py`)

The body translator is selected lazily on first attempt; it is constructed
once per request and threaded through every attempt because it carries
the `TranscodeContext` (id map, loss warnings).

Add a private attribute to `RequestCoordinator.__init__`:

```python
self._transcoder_policy: TranscoderPolicy = (
    app.state.transcoder_policy if app is not None else TranscoderPolicy()
)
```

Better: pass it via constructor alongside the existing `catalog`, `router`,
etc. The constructor signature change is documented in the PR.

Inside `execute()`, after `_select_and_persist_attempt`:

```python
transcoder = select_transcoder(
    client_protocol=context.protocol,
    upstream_protocol=context.upstream_protocol,
)
if transcoder is not None:
    try:
        payload = json.loads(context.body_for_upstream)
    except (json.JSONDecodeError, ValueError):
        # leave body alone; existing path will pass through verbatim
        payload = None
    if isinstance(payload, dict):
        translated, warnings = transcoder.encode_request(
            payload, transcode_context
        )
        new_body = encode_json_body(translated)
        context.upstream_body = new_body
```

The `upstream_body` field already exists on `ProxyRequestContext`
(`coordinator.py:133`) and is consumed via `context.body_for_upstream`
(line 137). The wiring is purely additive.

`_execute_non_streaming` post-success:

```python
if transcoder is not None:
    try:
        upstream_payload = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        upstream_payload = None
    if isinstance(upstream_payload, dict):
        translated, _warnings = transcoder.decode_response(
            upstream_payload, transcode_context
        )
        body = encode_json_body(translated)
```

For non-retryable errors in `_execute_non_streaming`
(`coordinator.py:944`) and the parallel branch in `_execute_streaming`
(line 1148), call `transcoder.reencode_error` instead of passing
`resp_body` through verbatim.

### 5. Preflight context-limit checks

`_check_context_limits` (`api/proxy_request.py:150`) currently receives
`endpoint.protocol`. Phase 2 does **not** change this — the client asked
for a particular `max_tokens` and we honour the client's limit. Phase 4
introduces a second pass on the upstream payload when transcoding is
active. For now, the preflight still uses the client payload.

### 6. Structured logging

`execute()` emits a structured log at completion:

```python
if transcode_context.loss_warnings:
    logger.info(
        "transcode.loss_warnings request_id=%s "
        "client=%s upstream=%s warnings=%s",
        context.request_id,
        context.protocol,
        context.upstream_protocol,
        transcode_context.loss_warnings,
    )
```

The warning emission goes through the project's standard JSON logger
(`src/eggpool/logging.py`).

## Validation

After implementation:

```bash
uv run ruff format --check src/ tests/
uv run ruff check src/ tests/
uv run pyright src/
uv run pytest tests/unit/test_transcoder/ -v
uv run pytest tests/integration/test_transcode_body.py -v
uv run pytest tests/                                  # full suite
```

Acceptance criteria:

- Every translator has at least 20 unit tests covering common shapes,
  edge cases, and loss-warning emission.
- One integration test per direction:
  - OpenAI client → mocked Anthropic upstream: request bytes match the
    translated shape; response bytes match the OpenAI shape; usage
    recorded; status 200.
  - Anthropic client → mocked OpenAI upstream: mirror.
- One error integration test per direction: mocked upstream returns 400
  with the canonical error envelope; client receives the correct
  re-rendered envelope with status preserved.
- `TranscodeContext.loss_warnings` is populated correctly for every
  drop/clamp case.
- Existing tests remain green with zero changes to non-transcoder tests.

## Definition of done (phase 2)

- All files in "Files to create" exist with passing tests.
- `RequestCoordinator.__init__` accepts a `transcoder_policy` parameter
  with a default that preserves today's behaviour.
- `select_transcoder` is the single source of truth for translator
  dispatch.
- The MiniMax International scenario works against a mocked upstream in
  `tests/integration/test_transcode_body.py::test_openai_to_anthropic_minimax`.
- Roadmap updated; phase 2 marked complete.