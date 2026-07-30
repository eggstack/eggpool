# Residual Upstream, Streaming, and Hot-Path Hardening Roadmap

Date: 2026-07-30
Status: implementation handoff
Plan: 045

Planning baseline:

- `216e615d75269cc1471a920ae81ece9ef2d21802`

Related historical plans:

- `plans/022-upstream-error-isolation-and-hotpath-hardening-roadmap.md`
- `plans/031-upstream-hardening-corrective-roadmap.md`
- Plans 032 through 038

This roadmap supersedes only the closure claims from those plans that conflict with the residual defects identified at the planning baseline. It does not discard correct implementation already present in the failure-effects classifier, model quarantine, database recovery, retained finalization supervisor, provider client pool, runtime generation factory, or observability systems.

## Purpose

Close the remaining request-local compatibility, streaming termination, cancellation cleanup, and data-plane hot-path defects without broadening Eggpool into production-scale infrastructure that is inappropriate for its private SBC deployment target.

The motivating production symptoms are:

1. OpenCode Go MiniMax-M3 may reject a client-supplied thinking level even though native MiniMax accepts it.
2. One upstream validation failure can leave enough runtime or durable state inconsistent that unrelated proxy traffic degrades until restart or database repair.
3. Native MiniMax streams can end without a visible error and appear to have silently dropped.
4. Transcoded streams and provider-bound request transforms perform avoidable duplicate parsing, serialization, allocation, and database work.

The final state must make an unsupported thinking control request-local, make every selected attempt terminate exactly once, distinguish valid protocol completion from premature clean EOF, expose provider-specific timeout evidence, and reduce measured dispatch/streaming overhead without removing capability.

## Governing principles

1. Correctness precedes optimization.
2. No upstream compatibility failure may poison unrelated account, model, circuit, reservation, routing, or database state.
3. A terminal request outcome has exactly one owner.
4. Cancellation cannot interrupt required cleanup after durable selection.
5. A socket EOF is transport completion, not automatically protocol completion.
6. Provider timeout changes must be based on classified observations, not speculation.
7. The provider-bound request body is decoded once and serialized once after final mutation.
8. Each upstream SSE byte is framed once, even when transcoding and usage observation are both active.
9. Performance changes require before/after measurements through the real Eggpool proxy path.
10. No phase may add broad CI matrices, evidence bureaucracy, or release automation.

## Non-goals

- Replacing SQLite.
- Rewriting the router or quota model.
- Replacing HTTPX/httpcore.
- Adding new providers unrelated to the defects.
- Retrying after downstream bytes have been emitted.
- Treating every provider that omits a terminal marker as broken without a compatibility policy.
- Raising the global timeout without provider evidence.
- Enabling the dispatch writer by default solely for this roadmap.
- Adding a second request model beside `ProviderBoundRequest`.
- Reintroducing duplicate JSON or SSE parsers for observability.
- Broad dashboard redesign.
- Expanding ordinary CI beyond the repository's reduced verification model.
- Requiring live MiniMax credentials for the deterministic acceptance suite.

## Phase sequence

### Plan 046 — Provider Thinking-Control Normalization and Contract Resolution

Correct fixed/effort contract adaptation, contract precedence, and OpenCode Go compatibility matching. Every supported input spelling must be deterministically rejected, dropped, or mapped before upstream dispatch.

Primary ownership boundary: provider thinking-control contract resolution and payload adaptation only.

### Plan 047 — Single Terminal Owner and Cancellation-Safe Cleanup

Remove double-finalization paths and move complete post-selection cleanup into a retained, idempotent terminal job. Ensure database state, quota reservations, active counts, and circuit probe ownership converge under cancellation and failure injection.

Primary ownership boundary: terminal lifecycle ownership and cleanup only.

### Plan 048 — Protocol Completion and Premature EOF Classification

Track protocol terminal events and distinguish complete streams, clean premature EOF, malformed/incomplete EOF, transport exceptions, and client cancellation. Do not mark an incomplete stream completed merely because HTTPX iteration ended normally.

Primary ownership boundary: streaming completion semantics only.

### Plan 049 — Provider Timeout Policy and Stream Diagnostics

Separate first-byte, inter-chunk idle, connect, write, and pool timeout semantics where needed; expose structured provider-specific diagnostics; tune MiniMax only after evidence distinguishes idle timeout from premature EOF.

Primary ownership boundary: timeout configuration, classification, and observability only.

### Plan 050 — Provider-Bound Request Single-Decode Lifecycle

Make `ProviderBoundRequest` the authoritative decoded and serialized request state for post-selection transforms. Eliminate independent body decodes and re-encodes from thinking adaptation, synthetic cache controls, and stream-option injection.

Primary ownership boundary: request payload lifecycle only.

### Plan 051 — Unified SSE Framing and Transcoded-Stream Hot Path

Introduce one incremental SSE framing pass shared by terminal observation, usage extraction, and transcoding. Remove duplicate UTF-8/SSE parsing and benchmark output frame coalescing without changing protocol behavior.

Primary ownership boundary: streaming parser/transcoder hot path only.

### Plan 052 — Selection, Persistence, and Trace Hot-Path Reduction

Remove database awaits from the selection-claim lock, avoid diagnostic full-account scans for unsampled traces, and measure remaining lock/persistence overhead. Preserve all routing and durable-selection invariants.

Primary ownership boundary: pre-upstream selection and diagnostic overhead only.

### Plan 053 — Real-Runtime Validation, Performance Gates, and Exact-Head Closure

Exercise all prior phases through the real Eggpool application path with deterministic mock providers, cancellation/fault matrices, native and transcoded streams, bounded concurrency, and SBC-appropriate performance/soak profiles. Record exact-head closure without adding a new evidence apparatus.

Primary ownership boundary: integration validation, performance proof, documentation, and status closure only.

## Dependency graph

```text
045 roadmap
  |
  +--> 046 control normalization
  |
  +--> 047 terminal ownership
  |       |
  |       +--> 048 stream completion semantics
  |               |
  |               +--> 049 timeout policy and diagnostics
  |
  +--> 050 provider-bound request lifecycle
  |       |
  |       +--> 051 unified SSE framing
  |
  +--> 052 selection/persistence hot path

046 + 047 + 048 + 049 + 050 + 051 + 052
  |
  +--> 053 real-runtime validation and closure
```

Plan 046 and Plan 047 may begin independently. Plan 048 depends on the single-terminal-owner decisions from Plan 047. Plan 049 depends on Plan 048 because timeout evidence must not conflate premature EOF with idle timeout. Plan 050 may begin independently after its baseline measurements are captured. Plan 051 depends on Plan 048's terminal-state model and Plan 050's ownership boundaries. Plan 052 may proceed independently. Plan 053 is blocked on all implementation phases.

## Cross-phase invariants

Every phase must preserve these invariants:

- A generic upstream 400/409/422 does not suppress an account or model without typed evidence.
- Unsupported thinking controls create no shared health, quarantine, or backoff effect.
- Every selected attempt reaches one durable terminal outcome.
- Every reservation, active-count increment, and half-open probe acquisition is released exactly once.
- Failure effects are applied at most once per attempt identity.
- No retry occurs after downstream response bytes are emitted.
- A pre-body retry excludes the already-attempted account and remains bounded.
- The original client bytes remain available for audit-free request semantics but are never persisted as content.
- Request and stream buffers remain bounded.
- Diagnostics cannot terminate or alter an otherwise valid byte stream.
- Provider-specific configuration remains backward compatible unless a plan explicitly documents migration.
- Private SBC operation remains the design center; resource plateaus matter more than hyperscale throughput.

## Small-model execution contract

Each phase plan is deliberately narrow. Implementers must:

1. Read the roadmap and the current phase before editing code.
2. Confirm the planning baseline against current `main`; if relevant code has changed, record the new baseline in the handoff note.
3. Modify only the modules and tests required by the phase ownership boundary.
4. Avoid opportunistic naming, formatting, or architecture cleanup.
5. Add failing characterization tests before or with the fix.
6. Use deterministic barriers/fault seams instead of sleep-based correctness tests.
7. Preserve transition idempotency rather than relying on duplicate calls being harmless.
8. Record exact commands and numeric results in the commit/PR handoff.
9. Stop and document any newly discovered cross-phase defect instead of silently expanding scope.
10. Never weaken an assertion merely to match current behavior.

## Required validation layers

The roadmap requires four layers of proof:

### Unit and property-level proof

- Provider contract matching and adaptation decision tables.
- Terminal-event state transitions across arbitrary chunk boundaries.
- Idempotent cleanup and effect application.
- Timeout classification and configuration validation.
- Provider-bound mutation generation and serialization counts.
- SSE decoder bounds and malformed input behavior.

### Real application-path integration proof

Requests must enter Eggpool's ASGI endpoints and traverse real routing, selection, persistence, provider client, streaming generator, and finalization paths against deterministic local mock upstreams.

Direct helper calls may supplement but cannot substitute for the canonical integration cases.

### Performance proof

Measure native and transcoded requests through Eggpool with:

- warm and cold samples identified separately;
- dispatch p50/p95/p99;
- local pre-upstream p50/p95/p99;
- stream CPU time or process CPU proxy;
- JSON decode/encode counts;
- SSE framing counts;
- selection lock wait/hold;
- SQLite persistence latency;
- allocations or tracemalloc deltas where stable;
- exact request counts and concurrency.

### Resource/soak proof

Run bounded short and standard profiles on ordinary developer/CI hardware and an optional extended local profile. Report actual duration and request count. Validate plateau behavior for RSS, tasks, threads, descriptors where supported, finalization registry, active requests, reservations, trace queues, and SQLite contention.

## Roadmap acceptance criteria

- [ ] Plan 046 deterministically handles every supported thinking-control spelling and corrects contract ordering/matching.
- [ ] Plan 047 establishes one terminal owner and proves cancellation-safe complete cleanup.
- [ ] Plan 048 rejects or classifies premature clean EOF instead of recording success.
- [ ] Plan 049 distinguishes first-byte, idle, transport, and premature-EOF outcomes and tunes providers only from evidence.
- [ ] Plan 050 provides one decoded provider-bound payload and one final serialization.
- [ ] Plan 051 performs one SSE framing pass per upstream byte and preserves native/transcoded parity.
- [ ] Plan 052 removes database I/O from the selection-claim lock and avoids unsampled diagnostic scans.
- [ ] Plan 053 validates the combined system through the real runtime and closes on the exact final implementation head.
- [ ] No unrelated provider, routing, release, CI, or dashboard scope is added.
- [ ] No ordinary CI matrix or committed evidence bundle is introduced.

## Explicit rejection conditions

Do not close Plan 045 if any of the following remain:

- A fixed-thinking contract can forward `reasoning_effort`, `thinking.type`, `thinking.effort`, `thinking.budget_tokens`, or `thinking_budget` contrary to policy.
- An unsupported effort value shares the same internal result as an already-valid value.
- Contract priority can override a more-specific provider identity match.
- A streaming HTTP 4xx or local capability rejection can enter two terminal finalization paths.
- Cancellation can leave active counts, quota reservations, durable reservations, pending requests, or half-open probe ownership behind.
- A clean EOF without a protocol terminal event is always marked completed.
- MiniMax timeout changes are made without outcome classification data.
- Provider transforms independently decode or serialize the same body.
- The observer and transcoder independently frame the same SSE bytes.
- Account ID database lookup occurs while the global selection-claim lock is held.
- Unsampled requests scan all accounts solely to build a trace that will not be persisted.
- Performance claims bypass Eggpool's proxy endpoint or omit exact request counts.
- The final closure status references a commit before the last source or test change.

## Definition of done

This roadmap is complete when provider-control compatibility failures are request-local and deterministic; selected-attempt cleanup is single-owner and cancellation-safe; streams require protocol-valid completion or expose a classified incomplete outcome; timeout behavior is provider-specific and evidence-driven; request and SSE processing perform one authoritative parse lifecycle; selection avoids unnecessary serialized work; all behavior is proven through the real Eggpool runtime; and the exact final head passes the repository's reduced canonical checks without adding verification bureaucracy.