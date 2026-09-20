# Deep Dive: Request Lifecycle

Back to [Architecture](README.md)

`rust/src/server/inference.rs` accepts the public HTTP surfaces and hands
requests to `rust/src/coordinator/`; startup and route assembly remain in
`rust/src/server/mod.rs`. The coordinator prepares bounded canonical input,
resolves exact virtual aliases, selects a provider/account, persists request
and attempt identity, dispatches through the provider pool, adapts the response,
and finalizes durable state.

`rust/src/wire/ir.rs` is captured before provider adaptation. A selected wire
codec under `rust/src/wire/` encodes the provider request and decodes finite or
streaming responses. Native terminal evidence is required; transport EOF is
never treated as successful completion.

## Bounded admission and ownership

The production path in `rust/src/coordinator/endpoints.rs` receives the
already bounded Axum `Bytes`, parses and depth-checks it once, and selects
finite versus streaming from the same parsed object. `ParsedRequestBody` is
consumed by admission after direct inspection. Provider-qualified and virtual
model resolution mutates that object before one bounded serialization; the
final request is then constructed with `FiniteRequest::from_admitted` or
`StreamRequest::from_admitted`. Public slice-based admission/wire helpers
remain available for tests and compatibility callers.

For unchanged native forwarding, the coordinator uses the owned dispatch wire
path and clones only the `Bytes` handle, not its backing allocation. A borrowed
`AttemptPreparation` supplies headers, credentials, profile, and request data
through synchronous preparation; `PreparedUpstreamAttempt` owns all values
before provider submission is awaited. Cross-surface codecs and actual model
rewrites continue to allocate their required encoded representation.

Responses admission deliberately creates two bounded products: the canonical
semantic projection used by routing/accounting and a source-native preservation
envelope holding the already-parsed request JSON plus redacted feature facts.
Native Responses routing forwards the original validated bytes when possible;
an alias target rewrites only `model` and compact-serializes the preserved
object. Unknown/future input items and non-function tool definitions therefore
survive native forwarding without expanding `CanonicalMessage` or
`CanonicalContentBlock`; Responses custom/freeform tools are the deliberate
portable exception and retain their kind in the canonical projection.
Cross-surface preparation consults the feature facts and rejects native-only
semantic blockers before provider submission.

The stateless Responses policy is enforced at admission as well as the HTTP
adapter: `store` may be omitted or false, while `store: true`,
`previous_response_id`, conversation references, and background execution are
rejected locally.

### Compaction as an explicit operation

`POST /v1/responses/compact` is a bounded distinct operation owned by the
same coordinator path, not an ordinary Responses alias. `InferenceOperation`
(`Generate` vs `Compact`) selects compact admission
(`admit_compact_parsed_request` after the one endpoint parse; the public
`admit_compact_request` remains a slice-compatible wrapper): finite-only,
history `input` required, trigger rejected, same stateless and body bounds),
native compact preparation (source-native preservation plus EggPool-owned
model rewrite over the provider-owned compact path), and
compact result validation (bounded replacement-history object returned
unchanged with opportunistic usage extraction; semantic failures are never
success). Routing filters to natively compact-capable Responses surfaces
before submission — accounts without a qualified target are skipped without
upstream I/O — while retry budget, health effects, usage accounting,
cancellation, and finalization ownership are shared with generation. There is
no translated compaction fallback and no persisted conversation state. v2
`compaction_trigger` items on `POST /v1/responses` require explicit native
v2 capability and otherwise fail with `UnsupportedSemanticFeature`; they are
never treated as user text. Native no-rewrite compact dispatch transfers the
owned ingress `Bytes` handle; provider-qualified or virtual model resolution
serializes once only when the EggPool-owned `model` field changes.

### Streaming ownership

The streaming coordinator is an internal package with an explicit handoff
boundary:

```text
server/Axum
   -> streaming::coordinator::StreamingCoordinator
      -> provider transport
      -> wire::WireStream
   -> streaming::execution::StreamingExecution
      -> retained finalization/accounting
```

`StreamingCoordinator` owns route claims, provider submission, response-header
and first-byte timers, and all retry/alternate-wire decisions that can happen
before the downstream response is returned. Returning `StreamingExecution`
closes that retry window permanently. `StreamingExecution` owns the one live
provider body, incremental chunk handoff, idle timeout, downstream cancellation,
and drop behavior. `terminal.rs` interprets `WireStream` terminal summaries and
builds bounded finalization facts; it does not parse provider events itself.

`WireStream` selects an explicit output mode from the client/upstream surface
compatibility path. Native Responses-to-Responses streams feed each raw chunk
through the incremental SSE observer, then forward the original bytes without
reconstructing known events; valid unknown event types are therefore preserved.
Canonical adaptation uses a stateful encoder owned by that stream. It retains
only bounded active message, reasoning, and tool-call buffers, allocates a
stable response ID and output indexes, maps Responses item IDs back to
invocation `call_id`s, and emits completed output items before the terminal
response. Freeform calls use the declared per-request tool map to emit
`custom_tool_call` items; client-executed deferred search uses the same
declaration scope to emit authoritative `tool_search_call` items with distinct
item/call IDs. Native and translated Responses paths both require
`response.completed` for success.

Retries and alternate wire negotiation consume one shared bounded submission
budget and are structurally unavailable after handoff. There is no whole-stream
deadline, complete-stream buffer, or EOF-as-success shortcut: SSE completion
requires wire terminal evidence, while non-SSE pass-through preserves its
legacy EOF behavior. Post-handoff failures are finalized rather than silently
replayed. Cancellation, database ambiguity, and generation retirement preserve
durable ownership and fail closed.
