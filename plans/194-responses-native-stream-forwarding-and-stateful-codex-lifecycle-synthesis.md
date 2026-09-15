# Plan 194: Responses native stream forwarding and stateful Codex lifecycle synthesis

> **Status:** READY FOR IMPLEMENTATION
>
> **Baseline:** Eggpool `main` at `51d770e16598cdea76ed56670f68b885f7f68ed5`
>
> **Parent:** Plans 192–193
>
> **Scope:** make Eggpool's streaming Responses behavior correct for current Codex without sacrificing provider-neutral translation. Add an observe-and-forward native mode for Responses->Responses streams and a bounded stateful Responses encoder for translated streams.

## Problem statement

The current streaming runtime always translates when a `WireStream` exists:

```text
provider bytes
  -> StreamEventDecoder
  -> CanonicalEvent(s)
  -> stateless encode_client_event()
  -> downstream bytes
```

This is implemented across:

- `rust/src/wire/stream.rs`;
- `rust/src/wire/runtime.rs`;
- `rust/src/coordinator/streaming/execution.rs`.

That is appropriate when the upstream surface differs from the client surface. It is unnecessarily destructive when both sides already speak Responses.

It also cannot synthesize a fully correct Responses lifecycle from another protocol because the downstream encoder has no per-response state.

At the current Codex baseline, the parser behavior is unambiguous:

- `response.output_item.done` is deserialized into the durable `ResponseItem` consumed by the turn loop;
- ordinary `response.function_call_arguments.delta` and `.done` are intentionally not the authoritative function-call completion path;
- reasoning summary deltas are ignored unless required metadata such as `summary_index` is present;
- `response.completed` is the required success terminal;
- EOF before `response.completed` is an error.

Current Eggpool does not meet that contract for translated function calls. `ToolCallEnd` currently encodes to no Responses frame.

OpenCodex demonstrates the missing bridge state. Its translator keeps a response ID, monotonically increasing sequence/output indexes, independent Responses item IDs and call IDs, argument/text/reasoning buffers, and emits complete `response.output_item.done` objects before the terminal response.

---

# Design decision

Create two explicit streaming modes.

## Mode 1 — Native observe-and-forward

Use when:

```text
client surface == Responses
upstream surface == Responses
no response-body transformation is required
```

The upstream SSE bytes are forwarded unchanged after bounded framing/validation, while the decoder observes them to maintain:

- terminal status;
- usage;
- response ID where useful;
- malformed-stream evidence;
- coordinator finalization semantics.

Unknown/future valid Responses events are forwarded even when Eggpool has no canonical event for them.

## Mode 2 — Canonical translate-and-synthesize

Use when the upstream surface differs from the client surface or Eggpool must deliberately transform stream semantics.

The upstream stream is decoded into canonical events, then a **stateful per-stream client encoder** produces a valid Responses lifecycle.

Do not attempt to make one stateless function serve both modes.

---

# Workstream 1 — Introduce an explicit stream output mode

Primary files:

- `rust/src/wire/runtime.rs`;
- `rust/src/coordinator/streaming/execution.rs`;
- `rust/src/wire/stream.rs`.

Replace the implicit assumption that `wire: Some(WireStream)` always means decode/re-encode.

A suitable model is conceptually:

```rust
enum StreamForwardingMode {
    NativeObserved,
    Translated,
}
```

or an equivalent field owned by `WireStream`/`ActiveStream`.

`WireProfileFlags` may expose a stream-native-passthrough capability analogous to request `body_passthrough`, but keep the decision derived from compatibility path and selected surfaces rather than provider-name special cases.

## 1.1 Native observer behavior

For each provider chunk in native mode:

1. feed the bytes into the existing incremental SSE decoder/observer;
2. if the chunk/framing is acceptable, forward the original provider bytes, not reconstructed events;
3. update terminal/usage state from decoded canonical evidence;
4. preserve unknown events that the decoder elects not to canonicalize;
5. on malformed framing, use the existing translation/malformed-stream failure policy rather than forwarding known-invalid data as successful output.

The observer may canonicalize only the event types Eggpool needs for lifecycle/metrics. It must not require every event to have a canonical representation.

Do not buffer the whole stream. Chunk splitting may differ from event framing, so rely on the existing incremental SSE parser for observation while retaining raw chunks for forwarding.

## 1.2 Terminal semantics

Native forwarding must retain the current strict Responses success rule:

- successful terminal evidence is `response.completed`;
- `response.failed`/`response.incomplete` remain non-success terminals as currently classified;
- raw EOF without a valid terminal remains premature/failed;
- `[DONE]` alone must not replace `response.completed` for current Codex compatibility.

The coordinator must not downgrade native passthrough to the legacy non-SSE EOF-completion policy merely because bytes are forwarded unchanged.

## 1.3 Usage and accounting

If usage is present in the native terminal response, keep extracting it through the observer for Eggpool metrics. Native pass-through should not trade protocol fidelity for loss of accounting.

If an unknown event cannot be canonicalized, forward it and ignore it for metrics unless it is malformed. Do not classify an unknown forward-compatible event as an upstream failure.

---

# Workstream 2 — Replace the stateless downstream event encoder with per-stream state

Primary file: `rust/src/wire/stream.rs`.

Current API:

```rust
encode_client_event(client_surface, &CanonicalEvent)
```

cannot accumulate function arguments, create a coherent output item, or maintain Responses indexes. Introduce a stateful encoder owned by one `WireStream`, for example:

```rust
struct ClientStreamEncoder {
    surface: ClientSurface,
    responses: Option<ResponsesStreamEncoderState>,
}
```

Keep Chat/Anthropic behavior as simple as it is today. The stateful machinery is primarily needed for Responses output.

A conceptual Responses state object should own bounded fields comparable to:

```text
response_id
sequence_number
next_output_index
active assistant item
active reasoning item(s)
active tool calls keyed by canonical call ID
per-call generated item ID
per-call accumulated argument buffer
terminal-emitted flag
```

Use existing request/stream bounds or introduce narrow explicit limits for accumulated translated text/tool arguments. Do not create unbounded collectors.

The encoder does not need to retain the entire final response. It needs only active-item state and what is required to emit each authoritative completed item.

---

# Workstream 3 — Correct ordinary function-call synthesis

This is P0 for Codex.

For a canonical translated function call, Responses output must use two identities:

- `call_id`: canonical tool invocation identity used to pair the later tool result;
- `item.id`: Responses output-item identity, generated locally when the upstream protocol has no Responses item ID.

Do not reuse one string for both merely because the current canonical event has one `id`.

## 3.1 Start

On the first canonical tool-call event for one call:

- allocate a stable Responses item ID, e.g. a locally generated `fc_...`;
- retain canonical `call_id` separately;
- retain tool name;
- allocate an `output_index`;
- initialize a bounded arguments buffer;
- emit an appropriate `response.output_item.added` item with empty/current arguments.

## 3.2 Argument deltas

Append canonical argument fragments to the bounded buffer.

It is useful to emit `response.function_call_arguments.delta` for UI/progressive clients, but current Codex does not use those frames as the authoritative completed tool call. Correctness therefore cannot depend on them.

When a canonical tool end is reached, optionally emit `response.function_call_arguments.done` with the complete argument string, then **must** emit:

```json
{
  "type": "response.output_item.done",
  "output_index": 0,
  "item": {
    "id": "fc_...",
    "type": "function_call",
    "call_id": "call_...",
    "name": "...",
    "arguments": "{...complete JSON...}",
    "status": "completed"
  }
}
```

Only then may the stream proceed toward `response.completed`.

## 3.3 Incomplete/error closure

If the upstream terminates with an incomplete/failure condition while a call is open:

- do not synthesize a falsely completed tool invocation;
- if a Responses incomplete item is emitted, mark its item status consistently with the terminal condition;
- ensure Codex cannot execute a half-issued call merely because an argument buffer existed.

Follow the semantic pattern current OpenCodex uses: completion and failure closure are distinct paths.

---

# Workstream 4 — Decode Responses function calls consistently for cross-surface clients

Native Responses->Responses no longer depends on canonical re-encoding, but Responses upstreams may still be translated to Chat/Anthropic clients.

Current Responses decoder uses different identity preference across events:

- output item added prefers `call_id` then item ID;
- argument delta prefers `item_id` then `call_id`;
- argument done also uses item/call identifiers.

Because Responses item ID and `call_id` are distinct, one logical call can appear as two canonical identities.

Add Responses decoder state that maps:

```text
item_id -> call_id
```

and tracks active call metadata.

Canonical tool identity must be the invocation `call_id`, because that is what later `function_call_output.call_id` uses.

Also decode ordinary `response.output_item.done` when `item.type == "function_call"`. It is the authoritative complete call and provides a recovery path if deltas were absent/coalesced.

Avoid double-emitting a completed call when both delta lifecycle and output-item-done are observed. Add an explicit per-call completion flag.

This state belongs in the Responses stream decoder, not in the provider-neutral event enum.

---

# Workstream 5 — Assistant message item lifecycle

Translated Responses clients should receive both progressive text and an authoritative completed message item.

On translated assistant text:

1. allocate one message item ID/output index for the active assistant message;
2. emit `response.output_item.added` as appropriate;
3. emit `response.output_text.delta` events with the item/output/content indexes expected by the Responses grammar;
4. accumulate the bounded text needed for completion;
5. emit `response.output_item.done` with a complete assistant `message` item before terminal completion.

Current Codex displays text deltas, but its broader history/compaction machinery consumes completed output items. Do not rely on delta-only behavior.

If the existing canonical event stream cannot signal a message boundary before terminal, close the active message immediately before the terminal event. Multiple assistant segments/tool boundaries should close and advance output indexes deterministically.

---

# Workstream 6 — Reasoning lifecycle and required indices

Current Eggpool emits `response.reasoning_summary_text.delta` with only `delta`. Current Codex requires at least `summary_index` alongside the delta and current Responses events also carry item/output/sequence identity.

For translated reasoning:

- allocate a Responses reasoning item ID such as `rs_...`;
- allocate output index;
- emit `response.output_item.added` for the reasoning item;
- emit summary/raw reasoning deltas with required indexes (`summary_index`, `content_index` where applicable), item ID, output index, and sequence number;
- retain only the bounded content needed to create the final item;
- emit `response.output_item.done` for the reasoning item.

Never synthesize `encrypted_content` from plaintext reasoning. That field is provider-generated opaque continuation material. It is preserved only by the native Responses path unless a future provider adapter has a justified equivalent.

If the source provider exposes a provider-specific reasoning signature through existing canonical reasoning metadata, preserve it only under the policies already established by Plans 157–160; do not mislabel it as OpenAI encrypted reasoning.

---

# Workstream 7 — Stable Responses terminal object

The translated encoder must allocate a non-empty response ID at stream creation and use that same ID through all Responses lifecycle events.

`response.completed` must contain a coherent response object with at least the fields current Codex deserialization requires, including:

- non-empty `id`;
- completed status;
- usage when available.

Do not emit `id: ""` when upstream protocols lack a response identifier. Generate a local one at stream start.

Likewise, failure/incomplete terminal objects should use the same local response ID and consistent status/error evidence.

Only one terminal event may be emitted.

---

# Workstream 8 — Sequence/index correctness

For synthesized Responses streams, maintain monotonically increasing sequence numbers if the selected wire grammar/version emits them.

Maintain stable:

- response ID;
- output index per output item;
- content index within message/reasoning items;
- summary index within reasoning summaries;
- item ID;
- tool `call_id`.

Do not store these protocol-specific indexes on `CanonicalEvent` unless another wire surface has a real semantic need for them. They are downstream serialization state.

---

# Workstream 9 — Preserve boundedness and coordinator ownership

The stateful encoder introduces accumulation, so explicitly bound it.

At minimum bound:

- active assistant text retained for a final message item;
- active reasoning text retained for a final reasoning item;
- each active function/custom-tool argument buffer;
- number of simultaneously active tool calls;
- total encoder-retained bytes.

Use existing request/output limits where they are semantically appropriate. Otherwise define narrow stream translation limits and classify overflow as a translation/resource failure, not an upstream account-health failure.

Do not buffer arbitrary native Responses streams; native mode forwards raw chunks.

Preserve the coordinator rule that after downstream handoff, a midstream translation/transport failure is terminal and does not re-enter pre-handoff retry selection.

---

# Tests

## Current-Codex parser fixtures

Construct deterministic Responses SSE fixtures that reflect the current Codex parser contract.

Test at least:

1. `response.created` -> text -> completed assistant item -> `response.completed`;
2. function call added -> argument deltas -> argument done -> completed function-call item -> terminal;
3. function call emitted without argument deltas but with authoritative completed item;
4. two parallel function calls with distinct `item.id` and `call_id`;
5. reasoning summary with `summary_index` and completed reasoning item;
6. raw reasoning content with `content_index` when supported;
7. incomplete tool call followed by `response.incomplete`;
8. `response.failed`;
9. EOF before `response.completed`;
10. unknown valid Responses event in a native stream.

## Native pass-through proof

Feed an upstream Responses stream containing:

- a known event;
- an unknown future event with extra fields;
- a function call lifecycle;
- terminal usage;
- `response.completed`.

Assert downstream bytes preserve the event payloads rather than re-encoding them, while Eggpool still records terminal status/usage.

Exact network chunk boundaries need not be preserved, but if implementation forwards source chunks unchanged they naturally will be.

## Cross-surface synthesis proof

Use canonical fixtures from:

- OpenAI Chat Completions;
- Anthropic Messages;
- Gemini Interactions/generateContent.

Assert that the resulting Responses stream is acceptable to a small test parser that mirrors current Codex behavior: only `response.output_item.done` is authoritative for completed function calls.

Do not write tests that merely assert Eggpool's own encoder can decode its own output; that can hide shared mistakes.

---

# Expected source changes

Primary:

- `rust/src/wire/stream.rs` — Responses decoder state, stateful downstream encoder, native observer support;
- `rust/src/wire/runtime.rs` — stream mode selection and encoder ownership;
- `rust/src/coordinator/streaming/execution.rs` — forward raw native bytes while observing lifecycle;
- stream/coordinator tests.

Possible secondary:

- `rust/src/wire/codec.rs` if stream adapter types need a native-forwarding mode;
- `rust/src/wire/mod.rs` exports;
- `rust/src/wire/ir.rs` only if a genuinely cross-provider tool/reasoning semantic is missing;
- diagnostics types if native-vs-translated stream mode should be reported as a bounded scalar.

Do not create a second streaming coordinator.

---

# Acceptance criteria

1. Responses->Responses streams preserve source-native SSE events and payloads instead of reconstructing them.
2. Native streams are still parsed enough to detect terminal success/failure, usage, malformed data, and premature EOF.
3. Chat/Anthropic/Gemini->Responses streams use a stateful encoder.
4. Every translated ordinary function call ends with an authoritative `response.output_item.done` containing complete arguments, `call_id`, separate item ID, name, and completed status.
5. Parallel calls do not mix IDs or argument buffers.
6. Responses upstream->non-Responses translation consistently uses `call_id` as canonical tool identity and correctly associates item IDs with calls.
7. Translated assistant output ends with a complete assistant message item.
8. Translated reasoning summary events include required indexes and complete a reasoning item.
9. No translated successful terminal has an empty response ID.
10. A clean translated stream emits one and only one `response.completed`.
11. EOF before a terminal remains failure for both native and translated Responses.
12. Stream translation memory is explicitly bounded.
13. Unknown native Responses events survive same-surface routing.
14. Existing non-Responses streaming behavior and coordinator finalization tests remain green.

---

# Handoff implementation order

1. Add native observed stream mode and prove unknown-event preservation/terminal observation.
2. Introduce per-stream client encoder ownership without changing semantics yet.
3. Implement stable response/output/item ID allocation and sequence/index helpers.
4. Implement assistant message lifecycle.
5. Implement function-call accumulation and authoritative `output_item.done`.
6. Correct Responses decoder item-id/call-id mapping and completed-call decoding.
7. Implement reasoning item/index lifecycle.
8. Add boundedness/overflow handling.
9. Run cross-surface stream fixtures and coordinator terminal tests.
10. Only then proceed to Plan 195's Codex-level conformance harness.

The implementation should borrow the **state ownership pattern** demonstrated by OpenCodex, not its entire proxy architecture. Eggpool already has the right routing/coordinator boundaries; it only needs wire-fidelity state at the stream codec edge.