# Transcoded Stream Dispatch Optimization Plan

## Context

EggPool supports OpenAI-compatible and Anthropic-compatible request paths with transparent bidirectional transcoding. The current implementation already contains several important request-path optimizations: preflight request translation can be reused during coordinator dispatch, context/reservation calculations are precomputed before account selection, and upstream stream I/O is outside the account-selection lock. Despite that, observed behavior indicates that several concurrent transcoded streams can push dispatch overhead from low hundreds of milliseconds to roughly 1-2 seconds while many non-transcoded streams remain much cheaper.

The working hypothesis for this pass is that active transcoded streams are consuming enough event-loop CPU and scheduling budget to delay fresh dispatch. This is distinct from network latency and distinct from the selection lock itself. The likely hot path is per-upstream-chunk SSE parsing, JSON decoding/encoding, coroutine churn, and downstream send frequency inside `eggpool.request.coordinator._build_stream_generator()` and `eggpool.transcoder.streaming`.

This plan focuses on capability-preserving fixes before larger architectural changes. Preserve all currently supported transcoding capabilities: text streaming, tool-call deltas, tool-use/result history, thinking/reasoning deltas, usage extraction, error translation, pause-turn sentinel behavior, stream completion semantics, request finalization, cache/compression observability, and dashboard stats.

## Goals

Reduce dispatch overhead interference caused by active transcoded streams without reducing protocol compatibility.

Keep the request path semantically equivalent for clients and upstreams.

Avoid changing routing policy, quota accounting, cost accounting, provider selection semantics, or capability classification.

Make improvements measurable through focused microbenchmarks and an E2E concurrency regression test.

## Non-goals

Do not remove any Anthropic/OpenAI feature translation in this pass.

Do not migrate JSON backend here. That is covered by `plans/transcoded-json-backend-orjson.md`.

Do not add a worker-thread offload layer unless the simpler fixes fail to meet acceptance criteria. Thread offload can increase latency variance and should be treated as a later fallback.

Do not change user-visible API response shapes except for insignificant JSON whitespace/serialization formatting if tests confirm protocol clients accept it.

## Current hot-path findings to verify locally

In `_build_stream_generator()`, the coordinator creates an `IncrementalSSEObserver`, calls `observer.observe(chunk)` for every upstream chunk, and uses that observer's usage result during finalization.

The selected streaming transcoder also constructs its own `IncrementalSSEObserver` in `_BaseStreamingTranscoder.__init__()` and each concrete `feed()` calls `self._observer.observe(chunk)`.

Therefore, transcoded streams appear to run two incremental SSE parse/observe passes per upstream chunk: one for usage/finalization and one inside the transcoder. Native streams only run the coordinator observer. This is the highest-confidence optimization target.

The streaming transcoder interface is declared async (`feed()` and `flush()` return `await`able lists), but the implementations perform synchronous CPU work only: incremental UTF-8 parsing, string manipulation, `json.loads`, nested dict/list construction, and `json.dumps`. Awaiting a coroutine per chunk is unnecessary scheduler overhead.

The coordinator yields each translated SSE frame separately. Several translated events emit multiple frames per upstream chunk, increasing ASGI send frequency and event-loop churn. Coalescing translated frames per upstream chunk should preserve wire ordering while reducing scheduler pressure.

The streaming path parses `context.body_for_upstream` to inject OpenAI `stream_options.include_usage`, then the generator parses it again to infer `include_usage`. This can be computed once and threaded through the generator.

## Phase 1: Add instrumentation and fixtures before behavior changes

Add a small set of representative SSE fixtures under the tests tree. Include at least these cases:

- Anthropic text-only streaming response with `message_start`, `content_block_start`, multiple `content_block_delta` text frames, `message_delta` with usage, and `message_stop`.
- Anthropic thinking + text streaming response with `thinking_delta` frames when thinking translation is enabled.
- Anthropic tool-use streaming response with `content_block_start` for `tool_use`, multiple `input_json_delta` frames, `content_block_stop`, and `message_delta stop_reason=tool_use`.
- Anthropic `pause_turn` response requiring the synthetic OpenAI sentinel.
- OpenAI text-only streaming response with normal `choices[].delta.content`, final finish chunk, optional usage chunk, and `[DONE]`.
- OpenAI streaming tool-call response with split `tool_calls[*].function.arguments` across many chunks.

Add focused tests around `select_streaming_transcoder()` and `_build_stream_generator()` behavior so the stream output is compared as decoded SSE events rather than raw byte-for-byte strings where harmless JSON whitespace may later change.

Add a performance test marked with `pytest.mark.performance` that replays a fixed high-chunk-count stream through each transcoder and records:

- chunks processed per second,
- translated frames emitted,
- output bytes emitted,
- CPU time per 1,000 upstream chunks,
- optional allocation count if available with low overhead.

Add an E2E-style async load test with a mocked upstream or `respx` transport that keeps 5-8 transcoded streams active while sending additional small non-streaming or short-stream dispatches. Capture dispatch span metrics already exposed by the runtime dispatch recorder, especially total dispatch overhead, `selection_lock_wait`, `selection_locked`, JSON parse, transcode preflight, routing plan, and DB write spans.

This test should be written so it can run in CI at a modest scale and locally at a larger scale. Use markers to keep the heavier version opt-in.

## Phase 2: Remove duplicate stream observation from the transcoder path

Refactor `eggpool.transcoder.streaming._BaseStreamingTranscoder` so it no longer constructs or drives an `IncrementalSSEObserver` by default.

Options, in preferred order:

1. Remove the internal observer entirely if no production caller uses `StreamingTranscoder.usage`.
2. If tests or diagnostics still need the property, make usage collection optional with `collect_usage: bool = False` and default it off in `select_streaming_transcoder()`.
3. If a single observer must remain visible to both layers, pass the coordinator's observer into the transcoder so both consumers share one parser. This is more invasive and should only be used if option 1 or 2 breaks a real requirement.

Update `StreamingTranscoder` protocol accordingly. If `usage` remains, document that it may be a zero/default result unless `collect_usage=True`.

Keep usage accounting in the coordinator observer because finalization currently consumes `observer.usage` there.

Verify that removing the internal observer does not change:

- final request usage counters,
- normalized usage status,
- cache read/write token accounting,
- reasoning/thinking counters,
- request finalization outcome,
- stream diagnostics.

## Phase 3: Make streaming transcoders synchronous

Change the `StreamingTranscoder` protocol:

```python
class StreamingTranscoder(Protocol):
    def feed(self, chunk: bytes) -> list[bytes]: ...
    def flush(self) -> list[bytes]: ...
```

Update `OpenAIToAnthropicStreaming.feed/flush` and `AnthropicToOpenAIStreaming.feed/flush` to regular methods.

Update `_build_stream_generator()` to call `streaming_transcoder.feed(chunk)` and `streaming_transcoder.flush()` without `await`.

This is capability-preserving because no transcoder method currently awaits I/O. If any future implementation requires async I/O, it should not be placed in the per-chunk transcoder path without a separate design review.

Tests should assert the public behavior is unchanged for all streaming fixture cases.

## Phase 4: Coalesce translated output per upstream chunk

In `_build_stream_generator()`, replace per-frame downstream yields for translated chunks with one joined yield per upstream chunk when possible:

```python
out_chunks = streaming_transcoder.feed(chunk)
if out_chunks:
    yield b"".join(out_chunks)
```

Do the same for `flush()`.

Do not coalesce across upstream chunks in this pass. Coalescing only within a single upstream chunk preserves the current streaming granularity as closely as practical while reducing ASGI send calls.

Ensure `[DONE]` and terminal Anthropic `message_stop` events are still emitted in the correct order.

Add tests that intentionally trigger multiple output frames from one input chunk:

- OpenAI `[DONE]` to Anthropic stop message path.
- OpenAI tool-call finish to Anthropic `content_block_start` + `content_block_stop` + `message_delta` + `message_stop` path.
- Anthropic `pause_turn` to OpenAI sentinel + terminal finish + `[DONE]` path.

Assert decoded event sequence, not number of yielded byte chunks, unless a test specifically checks send coalescing.

## Phase 5: Thread `include_usage` once through the stream generator

In `_execute_streaming()`, when OpenAI upstream `stream_options.include_usage` is inspected or injected, compute a local boolean such as `upstream_include_usage`.

Extend `_build_stream_generator()` signature to receive this boolean, or add it to `ProxyRequestContext` if that is cleaner.

Remove the second JSON parse in `_build_stream_generator()` currently used only to determine include-usage behavior.

Preserve behavior:

- OpenAI upstream streams should request `include_usage=True` when absent.
- Existing explicit `include_usage=False` should remain honored if current behavior honors it. If current behavior forces true after injection, document and preserve the existing behavior unless product intent changes.
- Anthropic upstreams should not receive OpenAI `stream_options` fields.

## Phase 6: Compact/streamline standard-library JSON frame emission

Before the orjson migration, update streaming frame helpers to use compact JSON separators if they still use stdlib JSON:

```python
json.dumps(data, separators=(",", ":"))
```

Apply to `_anthropic_frame()` and `_openai_frame()` only if tests confirm no client relies on whitespace. SSE clients should parse JSON values and should not require whitespace preservation.

This reduces output bytes and some serialization work while preserving JSON semantics.

## Phase 7: Optional low-risk template cleanup

After the above changes, inspect p95 per-chunk CPU again. If still high, refactor common frame constructors into small helper functions that reduce repeated nested literal construction.

Do not hand-assemble arbitrary JSON in this pass. Direct byte-template assembly is tempting for `delta.content` frames but carries escaping and compatibility risk. Defer it unless measured profiles show dict construction/serialization remains dominant after orjson migration.

## E2E capability regression acceptance criteria

All existing transcoding tests must continue to pass.

OpenAI client to Anthropic upstream must preserve:

- request body translation for text messages,
- system extraction/translation,
- tool definitions and tool choice translation when feature enabled,
- assistant `tool_calls` history to Anthropic `tool_use`,
- tool result history to Anthropic `tool_result`,
- OpenAI `reasoning_effort` to Anthropic thinking budget when feature enabled,
- stream text deltas,
- stream tool-call deltas,
- stream thinking/reasoning deltas when enabled,
- `stream_options.include_usage` semantics,
- non-retryable error re-rendering.

Anthropic client to OpenAI upstream must preserve:

- request body translation for text messages,
- top-level system translation,
- Anthropic tools and tool choice translation when feature enabled,
- `tool_use`/`tool_result` history translation,
- response body translation,
- streaming text deltas,
- streaming tool-use deltas,
- pause-turn sentinel behavior,
- usage accounting and finalization,
- non-retryable error re-rendering.

Native OpenAI-to-OpenAI and Anthropic-to-Anthropic paths must remain unaffected. Native streams should not instantiate a streaming transcoder and should show no regression in output bytes or finalization behavior.

Request finalization must still release reservations and decrement active counts on completed, cancelled, retryable failure, non-retryable failure, and midstream error paths.

No request may remain `pending` after a completed/cancelled E2E stream test.

No active reservation may remain after completed/cancelled E2E stream tests.

Usage/cost/cache counters from streaming completion should match pre-change values for the same fixture streams.

Dashboard/runtime stats should still distinguish transcoded requests where currently recorded.

## Performance acceptance criteria

Use the new performance tests to compare against a baseline commit before the patch series.

At 5-8 concurrent mocked transcoded streams plus fresh dispatch probes:

- p95 fresh dispatch overhead should improve materially relative to baseline, target at least 30% reduction.
- p99 fresh dispatch overhead should not exceed baseline.
- `selection_lock_wait` and `selection_locked` should not increase; this pass should reduce event-loop pressure, not move work into the lock.
- Transcoded stream throughput should improve or remain neutral.
- Native stream throughput should remain neutral within noise.
- Output event sequence must be identical after decoding SSE frames.

For the microbenchmark:

- Per-1,000-chunk transcoder CPU time should improve by at least 20% after duplicate observer removal and sync feed/flush.
- Number of JSON parses per transcoded chunk should be reduced by one observer path.
- Number of downstream ASGI yields per translated upstream chunk should be <= 1 except passthrough/native paths and exceptional cases where the framework requires otherwise.

## Manual validation checklist

Run:

```bash
ruff check src tests
pyright
pytest -m request_path
pytest -m performance
pytest tests -k transcoding
```

Then run a local EggPool instance against mocked or real providers with:

- 1 native OpenAI stream,
- 1 native Anthropic stream,
- 5-8 OpenAI-client to Anthropic-upstream transcoded streams,
- 5-8 Anthropic-client to OpenAI-upstream transcoded streams if configured,
- repeated small non-stream dispatch probes during the active streams.

Inspect runtime dispatch metrics and stream diagnostics. Confirm overhead regression is reduced and no stream leaks or finalization retries appear unexpectedly.

## Rollback strategy

Keep changes localized to `eggpool.transcoder.streaming` and `eggpool.request.coordinator` where possible.

If behavior regressions appear, first disable output coalescing while keeping duplicate-observer removal. If usage regressions appear, temporarily restore observer sharing/collection behind a flag rather than reverting all stream hot-path changes.

## Deliverables

- Updated streaming transcoder without default duplicate usage observer.
- Synchronous `feed()`/`flush()` streaming transcoder interface.
- Coalesced translated output yields per upstream chunk.
- Single-pass `include_usage` computation for streaming setup.
- Compact stdlib JSON frame serialization if safe.
- New stream fixture tests.
- New performance/concurrency regression tests.
- Documentation note in `docs/transcoding.md` or architecture notes if the internal streaming design changes materially.
