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

Responses admission deliberately creates two bounded products: the canonical
semantic projection used by routing/accounting and a source-native preservation
envelope holding the already-parsed request JSON plus redacted feature facts.
Native Responses routing forwards the original validated bytes when possible;
an alias target rewrites only `model` and compact-serializes the preserved
object. Unknown/future input items and non-function tool definitions therefore
survive native forwarding without expanding `CanonicalMessage` or
`CanonicalContentBlock`. Cross-surface preparation consults the feature facts
and rejects native-only semantic blockers before provider submission.

The stateless Responses policy is enforced at admission as well as the HTTP
adapter: `store` may be omitted or false, while `store: true`,
`previous_response_id`, conversation references, and background execution are
rejected locally.

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
only bounded active message, reasoning, and function-call buffers, allocates a
stable response ID and output indexes, maps Responses item IDs back to
function invocation `call_id`s, and emits completed output items before the
terminal response. Native and translated Responses paths both require
`response.completed` for success.

Retries and alternate wire negotiation consume one shared bounded submission
budget and are structurally unavailable after handoff. There is no whole-stream
deadline, complete-stream buffer, or EOF-as-success shortcut: SSE completion
requires wire terminal evidence, while non-SSE pass-through preserves its
legacy EOF behavior. Post-handoff failures are finalized rather than silently
replayed. Cancellation, database ambiguity, and generation retirement preserve
durable ownership and fail closed.
