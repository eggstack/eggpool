# Phase 3 — Streaming translation

## Goal

Translate SSE streams in both directions so a streaming client (OpenCode
streaming completions, Claude Code streaming messages, `stream=true`
requests) sees correctly framed events from a transcoded upstream, with
usage and finalizer integration unchanged.

After this phase, the full request lifecycle works for streaming text
requests:

- OpenAI client with `stream=true` → Anthropic upstream: client sees
  OpenAI SSE frames (`data: {…}\n\n` and terminal `[DONE]`); usage
  recorded; `IncrementalSSEObserver` driven by upstream frames.
- Anthropic client with `stream=true` → OpenAI upstream: client sees
  Anthropic SSE events (`event: message_start`, `content_block_delta`,
  `message_delta`, `message_stop`); usage recorded.

## Scope

In scope:

- SSE frame translation in both directions for text-only streams.
- Backpressure-safe async generator that yields client-format bytes
  while feeding the upstream observer for usage extraction.
- Mid-stream error event handling (transcoded `error` event in client
  format before terminal close).
- Cancellation shield integration: the existing
  `asyncio.shield(asyncio.wait_for(..., timeout=10))` around streaming
  finalization must continue to protect the transcoder's final cleanup.
- Unit tests against canned SSE byte streams in both directions.
- Integration test using `respx` to mock upstream SSE responses and
  assert the byte stream the client receives.

Out of scope:

- Tool-call streaming. Anthropic `input_json_delta` reassembly is phase 6.
- Thinking/reasoning streaming. Phase 6.
- Mid-stream cancellation scenarios beyond what the existing shield
  already handles.

## Files to create

```
src/eggpool/transcoder/
└── streaming.py             # StreamingTranscoder (text-only v1)

tests/unit/test_transcoder/
├── test_streaming_openai_to_anthropic.py
├── test_streaming_anthropic_to_openai.py
└── test_streaming_error_events.py

tests/integration/
└── test_transcode_streaming.py
```

## Files to modify

```
src/eggpool/request/coordinator.py   # replace `yield chunk` in _build_stream_generator
src/eggpool/transcoder/__init__.py   # public exports
```

## Detailed design

### 1. SSE framing primer

The two protocols differ in five ways the transcoder must bridge:

| Aspect | OpenAI | Anthropic |
|---|---|---|
| Frame delimiter | one chunk per `data: <json>\n\n` | `event: <type>\ndata: <json>\n\n` |
| Terminal marker | `data: [DONE]\n\n` (literal sentinel) | `event: message_stop\ndata: {"type":"message_stop"}\n\n` |
| First chunk | `data: {"id":...,"object":"chat.completion.chunk",...}` | `event: message_start\ndata: {"type":"message_start","message":{...}}` |
| Content delta | `data: {"choices":[{"delta":{"content":"x"},...}]}` | `event: content_block_delta\ndata: {"type":"content_block_delta","index":N,"delta":{"type":"text_delta","text":"x"}}` |
| Stop reason | last chunk carries `finish_reason` | `event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"..."}}` |

Anthropic's events are **typed**; the transcoder must generate the
`event:` line per SSE spec. OpenAI has no event type line.

### 2. `StreamingTranscoder` interface (`src/eggpool/transcoder/streaming.py`)

```python
class StreamingTranscoder(Protocol):
    """Translate an upstream SSE stream into client-format bytes."""

    client_protocol: str
    upstream_protocol: str

    async def feed(self, chunk: bytes) -> list[bytes]:
        """Accept an upstream byte chunk and return zero or more
        client-format byte chunks to emit downstream.

        May return an empty list if the chunk contains no complete frame.
        May return multiple chunks if one upstream chunk contains many
        client chunks (e.g. an OpenAI delta produces one Anthropic
        message_delta + content_block_delta pair, or vice versa).
        """

    async def flush(self) -> list[bytes]:
        """Drain any buffered state at end-of-stream. Must emit a
        terminal frame in the client format and any trailing events
        the client expects."""

    @property
    def usage(self) -> StreamUsageResult:
        """The transcoder-observed usage extracted from upstream frames."""
```

The transcoder owns an internal `IncrementalSSEObserver` constructed with
the upstream protocol; `usage` is delegated to that observer. The observer
is also accessible directly (e.g. for finalizer integration) — but the
coordinator reads it via the existing `observer.usage` field to avoid
re-parsing.

### 3. `OpenAIToAnthropicStreaming`

Constructs an `AnthropicStreamUsageExtractor` internally (via the
observer) and re-emits Anthropic SSE events.

State machine:

```
upstream: data: {role, content: "x"}
  → emit:
    event: message_start
    data: {"type":"message_start","message":{"id":...,"type":"message","role":"assistant","model":...,"content":[],"usage":{...}}}
    event: content_block_start
    data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}
    event: content_block_delta
    data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"x"}}
```

Subsequent text deltas only emit `content_block_delta` events with the
same `index`. When a chunk carries `finish_reason`, emit:

```
event: content_block_stop
data: {"type":"content_block_stop","index":0}
event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"<mapped>","stop_sequence":null},"usage":{"output_tokens":<int>}}
event: message_stop
data: {"type":"message_stop"}
```

A chunk carrying usage (when `stream_options.include_usage: true` was
injected in phase 2) emits:

```
event: message_delta
data: {"type":"message_delta","usage":{"output_tokens":<int>}}
```

…between the prior `content_block_delta` and the `message_stop`.

The `id` field in `message_start.message.id` is preserved from the first
upstream chunk's `id`. The `model` is preserved from the first chunk's
`model`. `created` is dropped (no Anthropic equivalent; phase 6 may
synthesize one).

### 4. `AnthropicToOpenAIStreaming`

Constructs an `OpenAIStreamUsageExtractor` internally.

State machine:

```
upstream: event: message_start / data: {"type":"message_start","message":{"id":...,"model":...,"usage":{...}}}
  → emit:
    data: {"id":...,"object":"chat.completion.chunk","created":0,"model":...,"choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}

upstream: event: content_block_start / data: {"type":"content_block_start","index":N,"content_block":{"type":"text","text":""}}
  → no-op (already announced via role delta)

upstream: event: content_block_delta / data: {"type":"content_block_delta","index":N,"delta":{"type":"text_delta","text":"x"}}
  → emit:
    data: {"id":...,"object":"chat.completion.chunk","created":0,"model":...,"choices":[{"index":0,"delta":{"content":"x"},"finish_reason":null}]}

upstream: event: message_delta / data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{...}}
  → emit:
    data: {"id":...,"object":"chat.completion.chunk","created":0,"model":...,"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}
    if stream_options.include_usage was set:
      data: {"id":...,"object":"chat.completion.chunk","created":0,"model":...,"choices":[],"usage":{...}}
    data: [DONE]
```

The OpenAI stream MUST end with `data: [DONE]\n\n` regardless of whether
the upstream ended cleanly. This is the contract every OpenAI client
implements `await response.aiter_lines()` against.

### 5. Frame-boundary safety

`IncrementalSSEObserver` (`proxy/sse_observer.py:48`) already handles
arbitrary chunk boundaries. The streaming transcoder wraps the observer
and a small state machine. The transcoder buffers an upstream frame until
its JSON is complete, then translates and yields. This means the
transcoder **never holds more than one upstream frame** in memory —
backpressure flows naturally through the iterator contract.

When an upstream frame is malformed JSON, the transcoder emits a warning
in `TranscodeContext.loss_warnings` and skips the frame. v1 does not abort
the stream on a single bad frame; production traffic from major providers
does not emit malformed frames, so this is a graceful-degradation path.

### 6. Mid-stream error events

Anthropic SSE includes a typed `error` event:

```
event: error
data: {"type":"error","error":{"type":"overloaded_error","message":"..."}}
```

The transcoder captures it, emits a single OpenAI `data: {"error":{...}}`
event, and then `data: [DONE]` (or the equivalent Anthropic `message_stop`
on the reverse direction). The coordinator's existing
`_classify_upstream_error` does **not** fire because the response status
was already 200; the error is in-band.

For the forward direction (OpenAI error mid-stream), OpenAI clients
typically see a `data: {"error":{...}}` followed by `[DONE]`. The reverse
direction produces Anthropic `error` event + `message_stop`.

### 7. Coordinator integration (`_build_stream_generator`)

Today (`coordinator.py:1238-1248`):

```python
async for chunk in upstream_response.aiter_bytes():
    if first_byte_ms == 0.0:
        first_byte_ms = (time.monotonic() - reference) * 1000
    observer.observe(chunk)
    bytes_emitted = observer.bytes_emitted
    yield chunk
```

Phase 3 replaces this with:

```python
transcoder = select_streaming_transcoder(
    client_protocol=context.protocol,
    upstream_protocol=context.upstream_protocol,
)
async for chunk in upstream_response.aiter_bytes():
    if first_byte_ms == 0.0:
        first_byte_ms = (time.monotonic() - reference) * 1000
    observer.observe(chunk)            # usage extraction on upstream frames
    bytes_emitted = observer.bytes_emitted
    if transcoder is not None:
        for out_chunk in await transcoder.feed(chunk):
            yield out_chunk
    else:
        yield chunk

if transcoder is not None:
    for out_chunk in await transcoder.flush():
        yield out_chunk
```

The observer is **still constructed with `upstream_protocol`** (from
phase 1) and continues to feed the finalizer as today. Usage extraction
sees upstream frames verbatim. The transcoder is a separate concern that
operates on the same bytes for byte-emission purposes.

When `transcoder is None` (same-protocol request), the code path is
**identical to today** — single `yield chunk`. This guarantees zero
regression for native-protocol requests.

### 8. Cancellation and shield integration

The existing shield wraps the finalizer, not the generator. Phase 3
preserves this exactly: `transcoder.flush()` is called outside the
generator's normal yield loop, after the upstream is exhausted. If the
client cancels mid-stream:

1. The `async for chunk` loop is interrupted by `asyncio.CancelledError`.
2. The generator's `finally` block (existing) closes the upstream
   response and runs `finalizer.finalize` under the shield.
3. The transcoder state is discarded — there is no per-request state
   that must be cleaned up beyond closing the upstream response.

No change to the shield mechanism. The transcoder does not hold external
resources.

### 9. Header handling

Response headers from the upstream arrive before the body. The existing
`_execute_streaming` (`coordinator.py:1162`) computes `resp_headers` and
hands them to `_build_stream_generator`. When transcoding, the upstream
may emit `Content-Type: text/event-stream` and Anthropic-specific
headers (`request-id`, `anthropic-organization-id`). The transcoder does
not rewrite headers — it preserves what the existing
`filter_response_headers` (`proxy/client.py:97`) returns. Clients that
inspect Anthropic-specific headers on a transcoded response are out of
scope; the documentation will note this as a known limitation.

## Validation

After implementation:

```bash
uv run ruff format --check src/ tests/
uv run ruff check src/ tests/
uv run pyright src/
uv run pytest tests/unit/test_transcoder/test_streaming_*.py -v
uv run pytest tests/integration/test_transcode_streaming.py -v
uv run pytest tests/                                          # full suite
```

Acceptance criteria:

- Unit tests cover:
  - Multi-chunk upstream SSE with arbitrary byte boundaries (no
    UTF-8 corruption).
  - First chunk carries `id` and `model`; subsequent chunks reuse them.
  - Multiple text deltas concatenate correctly.
  - Usage chunk in OpenAI stream translates to Anthropic `message_delta.usage`.
  - Anthropic `message_delta` with `stop_reason` translates to OpenAI
    final chunk with `finish_reason` plus `[DONE]`.
  - Mid-stream Anthropic `error` event translates to OpenAI `data: {"error":...}` plus `[DONE]`.
  - Mid-stream OpenAI `data: {"error":...}` translates to Anthropic
    `event: error` plus `message_stop`.
  - Empty stream produces no client bytes (just terminal frame).
- Integration tests assert:
  - End-to-end OpenAI streaming request → mocked Anthropic upstream →
    OpenAI-shaped client bytes received.
  - End-to-end Anthropic streaming request → mocked OpenAI upstream →
    Anthropic-shaped client bytes received.
  - Usage is finalized correctly in both directions.
  - Existing streaming tests (`tests/integration/test_streaming_*`) remain
    green without modification.

## Definition of done (phase 3)

- All files in "Files to create" exist with passing tests.
- `_build_stream_generator` is the single seam where SSE translation is
  applied.
- Same-protocol requests have byte-identical output to today.
- Backpressure preserved: the slowest client still controls upstream
  read rate.
- Shield integration preserved.
- Roadmap updated; phase 3 marked complete.