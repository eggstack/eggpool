# Plan 048 — Protocol Completion and Premature EOF Classification

Date: 2026-07-30
Status: complete (implementation and verification closure)
Parent roadmap: `plans/045-upstream-streaming-hardening-hotpath-roadmap.md`
Depends on: Plan 047 terminal ownership
Planning baseline: `216e615d75269cc1471a920ae81ece9ef2d21802`

## Objective

Make streaming completion protocol-aware. Eggpool must distinguish a valid terminal stream from a socket that merely ended without raising an exception.

A clean EOF before the protocol terminal condition must not be finalized as `COMPLETED`. Before downstream response bytes begin it may be eligible for the existing bounded retry policy; after bytes begin it must become a visible, non-retryable midstream failure.

## Confirmed defect to close

The current streaming generator treats normal exhaustion of `httpx.Response.aiter_bytes()` as successful completion. The SSE observer notices OpenAI `[DONE]` but discards that fact and does not retain an Anthropic `message_stop` terminal state. Therefore a clean upstream close can produce a truncated downstream response, success metrics, and no structured error.

This phase addresses completion semantics. Provider timeout values are owned by Plan 049. Shared SSE parser optimization is owned by Plan 051.

## Ownership boundary

Primary modules:

- `src/eggpool/proxy/sse_observer.py`
- `src/eggpool/request/coordinator.py` streaming generator and outcome classification
- `src/eggpool/request/stream_diagnostics.py`
- narrow streaming transcoder terminal-state adapters needed for protocol parity
- focused stream-completion tests

Do not consolidate all SSE framing in this phase; expose the terminal-state contract in a form Plan 051 can later consume.

## Required completion model

### 1. Separate transport EOF from protocol completion

Track at least:

```python
@dataclass(frozen=True, slots=True)
class StreamCompletionSnapshot:
    saw_payload: bool
    saw_terminal_event: bool
    terminal_kind: str | None
    saw_usage_completion: bool
    incomplete_frame_at_eof: bool
    parser_error_count: int
    bytes_observed: int
```

The exact type may differ. It must be queryable after each feed and at EOF.

### 2. Protocol terminal conditions

#### OpenAI-compatible upstream

Canonical terminal evidence:

- `data: [DONE]`.

Additional provider-compatible completion evidence may be allowed only through explicit compatibility policy, not incidental absence of errors.

A final usage-bearing chunk is not by itself equivalent to `[DONE]` unless the provider policy explicitly documents that behavior.

#### Anthropic-compatible upstream

Canonical terminal evidence:

- SSE event `message_stop`.

The parser must retain the `event:` field and associate it with the frame. A `message_delta` stop reason is useful metadata but is not automatically equivalent to `message_stop` unless provider policy explicitly permits it.

#### Native pass-through and transcoded streams

Completion is determined from the upstream protocol, not the downstream/client protocol. A transcoder may emit a downstream terminal marker only after upstream completion is established or when it is translating an upstream protocol terminal event.

### 3. EOF classifications

At upstream iterator exhaustion, produce exactly one classification:

- `complete`: canonical/allowed terminal evidence observed;
- `empty_eof`: no payload and no terminal evidence;
- `premature_eof`: payload observed but no terminal evidence;
- `malformed_eof`: parser ended with an incomplete/oversized/discarded terminal frame or protocol-invalid terminal sequence;
- `compatibility_eof`: provider policy explicitly permits markerless completion and required compatibility evidence is present.

Do not reuse `completed` for `premature_eof` merely because usage was present.

### 4. Retry boundary

- If EOF is incomplete and zero downstream bytes have been emitted, convert to a typed pre-body retryable stream/open error and allow the existing bounded account retry policy.
- If any downstream bytes have been emitted, no retry is allowed. Submit one midstream terminal outcome through Plan 047's canonical owner.
- If the upstream produced headers but no payload and then EOF, classify according to provider policy and retry safety; do not hang indefinitely.

### 5. Client-visible behavior

After downstream streaming has started, HTTP status cannot be changed. Eggpool must:

- terminate the downstream iterator by raising a typed exception visible to the ASGI server/client where supported;
- record `MIDSTREAM_ERROR` rather than `COMPLETED`;
- include request ID in logs/diagnostics;
- avoid synthesizing a protocol terminal marker that would falsely imply success;
- preserve bytes already emitted.

For transcoded protocols, do not flush a synthetic successful terminal sequence after upstream premature EOF.

### 6. Provider compatibility policy

Some providers may omit canonical markers. Define a narrow provider stream-completion policy, initially with safe defaults:

- `strict`: require canonical terminal evidence;
- `compatible`: allow a provider-specific alternate terminal predicate;
- `permissive_observe`: record missing terminal evidence but temporarily preserve completion behavior for a named provider while gathering evidence.

Do not make global permissive behavior the default. Policy must be provider-bound and visible in diagnostics.

The implementation may begin with strict behavior for deterministic mocks and permissive-observe for providers whose real behavior has not yet been established, but the canonical MiniMax direct case must gather enough evidence in Plan 049 to select a final policy.

### 7. Terminal-event propagation through transcoders

Streaming transcoders must expose whether they observed/translated a terminal event. They must not manufacture `[DONE]` or `message_stop` merely because `flush()` was called at EOF.

Required cases:

- upstream canonical terminal frame split across arbitrary byte chunks;
- terminal frame and final content in the same transport chunk;
- EOF immediately after complete terminal frame without trailing newline;
- EOF during terminal frame;
- duplicate terminal frame;
- data after terminal frame.

Define deterministic behavior for duplicate/post-terminal frames, preferably ignore with diagnostic or classify malformed without forwarding contradictory success.

## Implementation sequence

### Workstream A — Extend frame observation

Teach the observer to retain `event:` and terminal state while keeping memory bounded. Add a completion snapshot API without changing usage extraction semantics.

### Workstream B — EOF decision function

Create one pure function mapping:

- upstream protocol;
- provider completion policy;
- completion snapshot;
- downstream bytes emitted;
- parser/transcoder state

into a typed EOF decision.

Unit-test this decision table separately from HTTPX.

### Workstream C — Coordinator integration

At normal iterator exhaustion, evaluate EOF decision before observer/transcoder success flush and before submitting final completion.

Ordering requirement:

1. drain/parser EOF state;
2. classify completion;
3. flush only transformations valid for that classification;
4. submit completion or incomplete terminal outcome through Plan 047 owner;
5. close upstream response.

### Workstream D — Transcoder terminal discipline

Ensure OpenAI↔Anthropic streaming transcoders translate real terminal frames and do not synthesize success on incomplete EOF.

### Workstream E — Diagnostics

Add bounded counters/outcomes for:

- complete with canonical marker;
- complete by provider compatibility policy;
- empty EOF;
- premature EOF before downstream bytes;
- premature EOF after downstream bytes;
- malformed/incomplete EOF;
- duplicate/post-terminal data.

Do not persist stream content.

## Required tests

### Observer tests

- `[DONE]` split at every byte boundary.
- `event: message_stop` split at every byte boundary.
- CRLF and LF variants.
- final frame without trailing newline.
- EOF during UTF-8 code point.
- EOF during field name/data line.
- oversized/discarded frame does not count as terminal.
- duplicate terminal and data-after-terminal behavior.

### Native integration tests

For OpenAI and Anthropic mock upstreams:

- valid terminal event → completed;
- content then clean EOF without terminal → midstream error;
- clean EOF before first downstream byte → eligible retry;
- empty response EOF → classified failure;
- transport exception before terminal → typed transport/midstream path;
- client cancellation remains client cancellation, not premature EOF.

### Transcoded integration tests

- OpenAI upstream to Anthropic client preserves terminal semantics.
- Anthropic upstream to OpenAI client preserves terminal semantics.
- incomplete upstream does not receive a synthetic downstream terminal marker.
- completion works when terminal frame is fragmented arbitrarily.
- usage extraction remains correct for complete streams.

### State cleanup tests

After every incomplete EOF:

- terminal lifecycle converges through Plan 047 owner;
- active count/reservation/probe state returns to baseline;
- no success backoff clearing occurs;
- no retry after downstream bytes;
- next unrelated request succeeds.

## Acceptance criteria

- [x] Normal HTTPX iterator exhaustion is no longer automatically equivalent to protocol completion.
- [x] OpenAI `[DONE]` and Anthropic `message_stop` are retained as terminal evidence.
- [x] Clean EOF with payload but no terminal evidence is classified as premature EOF.
- [x] Incomplete EOF is represented as retryable before downstream bytes and non-retryable after them.
- [x] Incomplete streams are finalized as `MIDSTREAM_ERROR`, never `COMPLETED`.
- [x] No synthetic downstream terminal marker is emitted after premature EOF.
- [x] Provider markerless compatibility is explicit, provider-bound, and diagnosable.
- [x] Terminal frames split across arbitrary chunk boundaries are handled correctly.
- [x] Observer buffers remain bounded under malformed/oversized input.
- [x] Native and transcoded streams share the same upstream completion decision.
- [x] Success backoff clearing occurs only for genuinely completed streams.
- [x] Stream diagnostics distinguish canonical completion, compatibility completion, empty EOF, premature EOF, and malformed EOF.
- [x] Focused tests pass without live provider credentials.

## Explicit rejection conditions

Do not close Plan 048 if:

- completion is inferred only from `aiter_bytes()` ending;
- final usage is treated as universal terminal evidence;
- the transcoder emits `[DONE]`/`message_stop` from `flush()` without upstream terminal evidence;
- incomplete EOF after emitted bytes is retried;
- strict/permissive behavior is global rather than provider-bound;
- tests cover only parser helpers and not the real streaming response path;
- timeout durations are changed here without Plan 049 evidence.

## Handoff record

Recorded:

- implementation commit SHA: recorded by the closing commit;
- completion snapshot: `StreamCompletionSnapshot` in `proxy/sse_observer.py`;
- EOF decision: `StreamEOFDecision` / `classify_stream_eof()` in `request/stream_completion.py`;
- provider default policy: `ProviderConfig.stream_completion_policy = "strict"`;
- diagnostics: canonical/compatibility completion, empty, premature-before-body,
  premature-midstream, and malformed EOF counters;
- verification: full focused/native/transcoded/smoke coverage plus local CI checks;
- unresolved provider convention: markerless compatibility remains opt-in and
  requires provider-specific evidence.
- provider default policy table;
- native/transcoded test matrix and counts;
- incomplete-before-byte retry proof;
- incomplete-after-byte no-retry proof;
- diagnostics counters added;
- any provider whose real terminal convention remains unresolved.

## Definition of done

Plan 048 is complete when Eggpool treats protocol terminal evidence—not the absence of a transport exception—as the definition of a completed stream; premature clean EOF becomes a classified retryable or midstream failure according to response-started state; native and transcoded output cannot falsely signal success; and all ownership state converges through the single terminal mechanism.
