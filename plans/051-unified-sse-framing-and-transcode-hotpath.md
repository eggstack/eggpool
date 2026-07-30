# Plan 051 — Unified SSE Framing and Transcoded-Stream Hot Path

Date: 2026-07-30
Status: implementation handoff
Parent roadmap: `plans/045-upstream-streaming-hardening-hotpath-roadmap.md`
Depends on: Plan 048 completion model and Plan 050 provider-bound ownership
Planning baseline: `216e615d75269cc1471a920ae81ece9ef2d21802`

## Objective

Parse each upstream SSE byte stream exactly once and share structured frames among terminal-completion observation, usage extraction, and protocol transcoding. Reduce CPU, allocation, and latency overhead for multiple concurrent transcoded streams without removing tool, thinking, usage, or protocol capability.

## Current problems to close

1. `IncrementalSSEObserver` performs its own UTF-8 incremental decode, CRLF normalization, line splitting, frame assembly, and selected JSON parsing.
2. Each streaming transcoder performs another independent UTF-8/SSE framing pass over the same bytes.
3. Completion detection introduced by Plan 048 would otherwise risk becoming a third interpretation of stream structure.
4. Per-input-chunk transcoder outputs are collected into lists and joined into a new byte string before yield, which may add allocations.
5. Parser behavior can drift between observer and transcoder for malformed lines, oversized frames, EOF, `event:` handling, and terminal markers.

## Ownership boundary

Primary modules:

- a new or extracted shared incremental SSE decoder module
- `src/eggpool/proxy/sse_observer.py`
- `src/eggpool/transcoder/streaming.py`
- `src/eggpool/request/coordinator.py` streaming loop
- focused parser parity, protocol transcode, allocation, and concurrency tests

Do not change provider timeout policy, terminal lifecycle ownership, routing, request-body transforms, or response protocol semantics except to correct parser divergence explicitly captured by tests.

## Required architecture

### 1. Shared incremental decoder

Create one bounded decoder that accepts bytes and emits structured frames:

```python
@dataclass(frozen=True, slots=True)
class SSEFrame:
    event: str | None
    data: str
    fields: tuple[tuple[str, str], ...] | None = None
    is_comment_only: bool = False
    byte_count: int = 0
```

The exact type may be leaner. Required behavior:

- incremental UTF-8 decoding with replacement/error diagnostics consistent with current safety policy;
- CRLF/LF/CR normalization across chunk boundaries;
- multiline `data:` assembly;
- `event:` retention;
- comments and unknown fields handled without terminating valid streams;
- bounded incomplete line and event memory;
- explicit EOF drain result;
- no JSON parsing inside the generic framing layer;
- no protocol-specific terminal decision inside the generic framing layer.

### 2. One owner of byte framing

The coordinator streaming loop must call the decoder once per upstream chunk. It then distributes frames to:

- completion tracker from Plan 048;
- usage observer/extractor;
- selected streaming transcoder when protocols differ.

Native pass-through may still yield original upstream bytes directly while frames are observed. Transcoded paths translate structured frames rather than reparsing raw bytes.

### 3. Frame-level observer API

Replace or supplement `observer.observe(bytes)` with:

```python
observer.observe_frame(frame)
observer.finish(eof_state)
```

Usage extraction may parse JSON only for frames that can carry relevant usage, preserving the current optimization that skips ordinary OpenAI content frames without `usage`.

Observer failure remains telemetry-only: malformed usage JSON cannot terminate an otherwise valid stream.

### 4. Frame-level transcoder API

Replace `feed(bytes)` parser ownership with a frame translation API:

```python
transcoder.translate_frame(frame) -> Iterable[bytes]
transcoder.finish(completion) -> Iterable[bytes]
```

Requirements:

- no internal SSE byte decoder/parser remains in concrete transcoders;
- tool-call delta state and reasoning/thinking state remain incremental;
- terminal output is emitted only in response to valid upstream terminal evidence per Plan 048;
- `finish()` handles buffered semantic state but cannot manufacture success after premature EOF;
- warnings remain bounded and attached to the transcode context.

### 5. JSON parse ownership

For each frame:

- generic decoder performs zero JSON parses;
- usage observer parses only usage-relevant frames;
- transcoder parses frames required for translation;
- where both need the same parsed JSON object, parse once and share a frame envelope containing optional lazy/cached parsed JSON.

A suitable envelope is:

```python
class DecodedSSEFrame:
    frame: SSEFrame
    def json_object(self) -> dict[str, Any] | None: ...  # cached
```

Do not eagerly JSON-decode comments, `[DONE]`, or frames irrelevant to both consumers.

### 6. Output emission strategy

Benchmark these options rather than assuming one is best:

1. yield each translated frame individually;
2. join all frames generated from one upstream chunk;
3. bounded coalescing by byte size and/or event-loop turn.

Selection criteria:

- semantic output equivalence;
- downstream ASGI send-call count;
- allocation/copy count;
- p95 inter-token delay;
- CPU under 5–8 concurrent transcoded streams;
- bounded buffering and cancellation responsiveness.

Do not add an unbounded coalescing buffer. Prefer a simple default and retain configurability only if measurements show a meaningful tradeoff.

### 7. EOF and error behavior

The shared decoder's EOF result must feed Plan 048's completion decision. Required distinctions:

- complete final frame without trailing blank line;
- incomplete line/frame;
- oversized discarded frame;
- invalid UTF-8 replacement count;
- terminal marker observed;
- data after terminal marker.

Transport exceptions still bypass normal EOF completion and enter Plan 047 terminal ownership with the correct Plan 049 diagnostic class.

## Implementation sequence

### Workstream A — Cross-parser characterization corpus

Build a reusable byte corpus covering:

- arbitrary chunk boundaries;
- CRLF/LF/CR;
- multiline data;
- comments/heartbeats;
- unknown fields;
- OpenAI content/usage/[DONE];
- Anthropic message/content/tool/usage/message_stop events;
- malformed JSON;
- invalid UTF-8;
- oversized lines/events;
- incomplete EOF;
- duplicate terminal/data after terminal.

Run current observer and transcoder behavior against the corpus and record intentional differences.

### Workstream B — Shared decoder

Implement and property-test the bounded framing layer. It must have no dependency on Eggpool protocol transcoders or usage models.

### Workstream C — Observer migration

Move usage and completion observation to frame-level APIs. Preserve usage/cost/cache semantics.

### Workstream D — Transcoder migration

Migrate OpenAI→Anthropic and Anthropic→OpenAI stream translators, including tool calls, reasoning/thinking, finish/stop reasons, usage, and compatibility aliases.

### Workstream E — Coordinator integration

Drive one decoder in the upstream iteration loop and distribute frames. Remove old duplicate parser instances/call paths.

### Workstream F — Output emission benchmark

Measure individual-yield, per-chunk join, and one bounded coalescing strategy. Select and document the simplest option meeting latency/CPU/memory gates.

### Workstream G — Cleanup

Delete dead parser code, duplicate frame types, and stale comments claiming a single pass where two passes remained. Keep compatibility shims only if external/public API requires them, with deprecation tests.

## Required tests

### Decoder tests

- property test arbitrary chunk partitioning yields identical frames;
- bounded memory under no-newline/oversized input;
- event/data assembly parity;
- EOF state correctness;
- comments and unknown fields preserved/ignored as designed;
- invalid UTF-8 cannot crash or grow unbounded.

### Protocol parity tests

For native and both transcode directions:

- text deltas;
- reasoning/thinking deltas;
- tool-call start/argument/stop;
- multiple concurrent tool calls;
- stop/finish reason mapping;
- usage and cache fields;
- final terminal markers;
- malformed irrelevant telemetry frame remains nonfatal;
- incomplete EOF emits no false success marker.

### Parse-count tests

- one SSE framing pass per upstream byte/chunk;
- no concrete transcoder byte parser constructed;
- shared lazy JSON object parsed at most once per frame when both consumers need it;
- ordinary pass-through content frames avoid unnecessary JSON parse for usage-only observation.

### Concurrency/performance tests

Use deterministic mock streams with 1, 5, and 8 concurrent transcoded requests. Record:

- process CPU or stable CPU proxy;
- request/stream completion latency;
- first-token and inter-token p95;
- allocations/tracemalloc where stable;
- bytes copied/coalesced;
- ASGI yield/send count;
- RSS delta;
- task count.

Compare to a baseline captured before migration on the same machine/run harness.

### Cancellation tests

Cancel during:

- partial UTF-8 sequence;
- partial frame;
- tool-call argument assembly;
- output coalescing;
- immediately after terminal frame.

All buffers/tasks and terminal ownership must converge.

## Performance acceptance targets

Targets are comparison gates, not universal hardware claims:

- native pass-through p95 dispatch/stream overhead regression no worse than 5%;
- transcoded 5–8 stream CPU reduction or throughput improvement of at least 15%, unless baseline proves duplicate framing was not a material contributor and a documented alternative metric improves materially;
- no p95 inter-token latency regression above 10%;
- no unbounded RSS/task growth;
- one framing pass and at most one shared JSON parse per frame are mandatory architectural gates regardless of timing variance.

If numeric targets are noisy on shared CI, keep them in the local runtime validation profile and retain deterministic operation-count gates in ordinary tests.

## Acceptance criteria

- [ ] One shared incremental SSE decoder owns byte framing.
- [ ] The usage observer consumes structured frames and no longer frames raw bytes independently.
- [ ] Streaming transcoders consume structured frames and contain no duplicate byte parser.
- [ ] Plan 048 terminal tracking consumes the same frame stream.
- [ ] Each upstream byte/chunk enters one framing pass.
- [ ] JSON parsing is lazy/cached when observer and transcoder need the same frame.
- [ ] Native and transcoded text, thinking, tools, usage, and terminal semantics remain correct.
- [ ] Premature EOF cannot trigger a synthetic terminal marker.
- [ ] Decoder and output buffers remain bounded.
- [ ] Output emission strategy is selected from measured alternatives.
- [ ] 5–8 concurrent transcoded streams show a material CPU/throughput improvement or an explicitly documented equivalent hot-path gain.
- [ ] Native pass-through performance does not regress beyond the defined comparison gate.
- [ ] Cancellation leaves no parser/transcoder/finalization residue.

## Explicit rejection conditions

Do not close Plan 051 if:

- observer and transcoder still each instantiate an incremental UTF-8/SSE parser;
- completion tracking reparses raw bytes separately;
- the generic decoder parses JSON eagerly;
- `flush()` manufactures terminal success after incomplete EOF;
- output optimization uses an unbounded buffer;
- benchmarks bypass Eggpool or omit concurrent transcoded streams;
- capability parity excludes tools or reasoning/thinking;
- performance claims are based only on microbenchmarks of the decoder.

## Handoff record

Record:

- implementation commit SHA;
- shared decoder/frame API;
- deleted duplicate parser paths;
- parser corpus/property-test results;
- protocol parity matrix;
- parse/framing operation counts;
- output emission alternatives and chosen result;
- native and 1/5/8-stream performance table;
- cancellation/resource convergence results.

## Definition of done

Plan 051 is complete when one bounded SSE framing layer feeds completion, usage, and transcoding; concrete transcoders no longer parse raw stream bytes; terminal and capability behavior remain correct; output emission is measurement-selected; and concurrent transcoded streams consume materially fewer CPU/allocation resources without native-path regression.