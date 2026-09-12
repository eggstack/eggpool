# Plan 183 — Streaming Coordinator Internal Decomposition

Date: 2026-09-12
Status: ready for handoff
Parent roadmap: `plans/178-rust-maintenance-consolidation-roadmap.md`
Planning baseline: `78a4da64de3c94a9f5fe29e05e6bdf40402bc16b`
Priority: P1/P2 maintainability / streaming reliability
Execution target: GPT-5.6 Luna/Sol or comparable implementation model

## Purpose

Split the approximately 131 KiB `coordinator/streaming.rs` implementation into cohesive internal modules while preserving the current streaming lifecycle exactly.

The current ownership boundary is correct and must remain intact: provider transport supplies bounded raw response I/O, `wire::WireStream` owns incremental protocol decoding/terminal evidence, and the streaming coordinator owns request attempts, timeout policy, retry-window closure, downstream handoff, cancellation, and durable finalization.

This plan is **not** permission to merge those layers or redesign streaming semantics.

## Current-state findings

The monolithic streaming coordinator contains several already-distinct conceptual sections:

- `StreamTimeoutPolicy` and duration conversion;
- `StreamPhase` lifecycle state;
- terminal outcome labels and bounded streaming diagnostics;
- `StreamRequest` admission/routing inputs and public errors;
- pre-handoff upstream response retention/exhaustion handling;
- coordinator execution/retry/claim/publication logic;
- `StreamingExecution` downstream body lifecycle;
- incremental chunk adaptation through `WireStream`;
- terminal evidence/EOF classification;
- downstream cancellation/drop handling;
- retained finalization registration and usage/accounting completion.

The file is large because one state machine accumulated many supporting types, not because the coordinator owns the wrong layer.

## Target module shape

Prefer an internal package such as:

```text
coordinator/streaming/
  mod.rs            # public API/re-exports + concise ownership documentation
  timeout.rs        # StreamTimeoutPolicy and timer helpers
  types.rs          # request/header/phase/error/outcome types
  diagnostics.rs    # bounded outcome counters/snapshots
  coordinator.rs    # pre-handoff selection/attempt/retry execution
  execution.rs      # StreamingExecution downstream chunk lifecycle
  terminal.rs       # EOF/terminal evidence -> finalization classification helpers
```

Exact filenames should follow actual cohesion after inventory. Do not split every enum or helper into its own file. Keep private details private and preserve the current public API through `streaming/mod.rs` re-exports where callers/tests rely on it.

## Non-negotiable streaming invariants

1. **Retry closes at downstream handoff.** Header/first-byte failures before `StreamingExecution` is returned may use the existing failure/retry engine. After handoff, no failure may transparently replay upstream.
2. **No whole-stream deadline.** Header, first-byte, and idle timers remain bounded policy; `max_lifetime_s` remains compatibility-parsed but is not converted into an absolute active-stream deadline.
3. **Transport EOF is not success by itself for SSE.** Success requires terminal evidence/classification from the wire runtime; empty, malformed, incomplete, provider-error, Responses failed/incomplete, and premature EOF remain distinct terminal outcomes.
4. **Non-SSE pass-through remains compatible.** Raw non-event-stream provider bodies keep the existing pass-through/EOF behavior.
5. **Streams remain incremental.** Do not buffer complete provider or downstream streams during decomposition.
6. **Cancellation never retries.** Downstream/client cancellation owns a terminal path and durable finalization, never an upstream replay.
7. **Drop cannot strand durable state.** Dropping `StreamingExecution` before natural completion must still register interrupted/cancelled retained finalization; dropping after a natural terminal must preserve the stored terminal truth.
8. **Wire owns protocol decoding.** Coordinator code may interpret `WireStream` terminal summaries but must not reproduce SSE/provider event parsing.
9. **Transport owns socket/TLS/proxy I/O.** Coordinator timers wrap transport phases but do not reimplement provider transport.
10. **Diagnostics remain bounded and secret-free.** No prompts, API keys, provider body text, raw chunks, session identities, or credentials in diagnostic state.

## Workstream A — Freeze the public streaming API

Inventory every public/re-exported item currently consumed outside the file, including:

- `StreamTimeoutPolicy`;
- `StreamPhase`;
- `StreamChunkError`;
- outcome constants used by tests/metrics;
- `StreamDiagnosticEvent` / snapshot types;
- `StreamClientHeaders`;
- `StreamRequest`;
- `StreamingCoordinatorError`;
- `StreamingCoordinator`;
- `StreamingExecution` and downstream result/finalization interaction.

Keep import paths stable through `coordinator::streaming` unless there is a compelling current internal reason to narrow visibility. Do not make internal helpers public just because code moved.

## Workstream B — Extract pure support types first

Move timeout policy, phase/error/request/header types, and bounded diagnostics before moving coordinator execution.

These pieces should remain mostly pure/data-oriented. Their extraction gives later execution code smaller imports without changing state-machine behavior.

Update stale migration-phase commentary to durable descriptions while retaining any historical compatibility behavior that still matters.

## Workstream C — Separate pre-handoff coordinator execution

Keep the pre-handoff request lifecycle together:

```text
admitted request
  -> select/claim route
  -> publish attempt
  -> dispatch provider request
  -> wait for headers
  -> classify header failure/retry
  -> prefetch first provider byte
  -> classify first-byte failure/retry
  -> construct StreamingExecution
  -> retry window permanently closed
```

All failure decisions that can still authorize another attempt must stay in this pre-handoff owner. Do not expose a generic retry method to post-handoff execution code.

Preserve last-real-upstream response pass-through on pre-handoff exhaustion.

## Workstream D — Isolate `StreamingExecution`

Move downstream-running state into a dedicated execution module with the fields/state needed to:

- mark downstream start;
- return one bounded encoded chunk at a time;
- apply idle timeout;
- feed bytes to exactly one `WireStream` instance;
- record bytes/timing/usage/terminal evidence;
- close/release the active transport body;
- register natural terminal or cancellation finalization;
- expose the current `StreamPhase`/diagnostics contract.

Do not make execution clonable if that would create multiple owners of the provider body/finalization state.

## Workstream E — Extract terminal/finalization helpers without changing authority

Pure classification helpers may live in a terminal module, but retained finalization remains coordinator/finalization-supervisor behavior.

Preserve the mapping between:

- canonical complete vs compatibility complete;
- empty EOF;
- premature EOF before body vs midstream;
- malformed stream;
- Responses failed/incomplete terminals;
- upstream midstream errors;
- client cancellation;
- translation failures;
- usage/cache accounting terminal data.

Terminal classification should consume `WireStream`/`TerminalEvidence` results, not parse raw event JSON again.

## Workstream F — Preserve server handoff contract

Re-run the Axum publication path after Plan 180 module moves. The HTTP adapter must still:

- receive accepted response headers from coordinator;
- mark downstream start at the existing semantic point;
- stream `next_chunk` incrementally;
- report downstream completion/error/cancellation exactly once;
- never trigger a second coordinator execution after response start.

Add no buffering layer merely to simplify ownership across files.

## Workstream G — Update current architecture documentation

Document the durable streaming ownership chain:

```text
server/Axum
   -> StreamingCoordinator (pre-handoff attempts/timeouts)
      -> provider transport
      -> WireStream (incremental protocol semantics)
   -> StreamingExecution (post-handoff body/cancellation)
      -> retained finalization/accounting
```

The documentation should emphasize retry-window closure and terminal-evidence ownership because those are the most safety-critical boundaries.

## Focused verification

The C008 suite is the primary behavioral contract and should be run repeatedly during extraction:

```bash
cargo test --manifest-path rust/Cargo.toml --test coordinator_c008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_stream -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_qualification -- --test-threads=1
```

Also run the later coordinator suites that compose streaming with broader failure/wire behavior when touched:

```bash
cargo test --manifest-path rust/Cargo.toml --test coordinator_c009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c010 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c011 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c013 -- --test-threads=1
```

Then run strict Clippy and the complete workspace/tooling verification baseline from Plan 178.

## Acceptance criteria

- `coordinator::streaming` is a cohesive internal module tree rather than one monolithic implementation file.
- existing public imports remain stable or are intentionally narrowed only for internal callers.
- pre-handoff retry logic is structurally separated from post-handoff `StreamingExecution` so transparent replay after handoff remains impossible.
- `wire::WireStream` remains the only owner of incremental SSE/provider terminal semantics.
- no complete stream is buffered and no whole-stream lifetime deadline is introduced.
- cancellation/drop behavior still durably finalizes exactly once.
- all C008 and relevant coordinator/wire regressions pass unchanged.
- no new framework/dependency is introduced.

## Handoff note

This is the highest semantic-risk refactor in Plan 178. Prefer several small move-only commits with focused C008 runs over a single large rewrite. If code cleanup requires changing a streaming invariant, stop and write a separate corrective plan rather than folding the behavior change into decomposition.