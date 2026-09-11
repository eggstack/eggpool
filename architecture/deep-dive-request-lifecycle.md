# Deep Dive: Request Lifecycle

Back to [Architecture](README.md)

`rust/src/server.rs` accepts the public HTTP surfaces and hands requests to
`rust/src/coordinator/`. The coordinator prepares bounded canonical input,
resolves exact virtual aliases, selects a provider/account, persists request
and attempt identity, dispatches through the provider pool, adapts the response,
and finalizes durable state.

`rust/src/wire/ir.rs` is captured before provider adaptation. A selected wire
codec under `rust/src/wire/` encodes the provider request and decodes finite or
streaming responses. Native terminal evidence is required; transport EOF is
never treated as successful completion.

Retries and alternate wire negotiation are classified before downstream handoff
and consume one shared bounded submission budget. Post-handoff failures are
finalized rather than silently replayed. Cancellation, database ambiguity, and
generation retirement preserve durable ownership and fail closed.
