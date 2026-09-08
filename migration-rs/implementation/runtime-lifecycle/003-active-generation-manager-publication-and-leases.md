# R003 — Active Generation Manager, Atomic Publication, and Request Leases

Status: closed; see [closure record](../../closure/runtime-lifecycle/003-status.md)

Source roadmap: `migration-rs/subsystems/runtime-lifecycle-roadmap.md`

Primary class: invariant/infrastructure

## Objective

Introduce the Rust active-generation manager and make generation acquisition/publication linearizable under concurrency. Replace static inference authority on the public inference path with explicit generation leases that live through finite completion and the full streaming body task.

R003 establishes the publication primitive used later by live rehash; it does not yet parse config changes or retire resources.

## Expected dependency

Add `arc-swap` as the single expected M8 runtime dependency unless an equally small standard-library design proves the same atomic publication semantics. Do not add a lifecycle framework or alternate async runtime.

## Generation slot

Wrap each `Arc<RuntimeGeneration>` in a `GenerationSlot` carrying process-local lifecycle metadata:

- generation id/digest prefix;
- accepting flag/state;
- active lease count;
- retirement/drain notification;
- publication/retirement timestamps or elapsed classes needed by diagnostics;
- close/retirement state placeholders for R004.

The slot, not the immutable generation, owns mutable lifecycle counters.

## Runtime manager

Implement a process-owned `RuntimeManager` with:

- `ArcSwap`/`ArcSwapOption` active slot;
- one very small synchronous publication/lease-claim critical section;
- an explicit admission-gate state and async notification/watch mechanism;
- monotonic publication epoch/generation id;
- shutdown flag;
- at most one staged/pending swap;
- retiring-slot collection placeholder for R004.

The hot path should not hold an async mutex across request work. A tiny sync lock around lease claim/publication is acceptable and preferred over a complicated lock-free protocol.

## Linearizable lease acquisition

`acquire()` must guarantee no old-generation lease can be admitted after the commit point.

A correct shape is:

1. wait while the short publication gate is closed;
2. enter the tiny publication read/claim section;
3. re-check gate/shutdown;
4. load the active slot and verify accepting;
5. increment its lease count before leaving the section;
6. return a `GenerationLease` containing the slot/generation `Arc`.

If publication races step 1-5, acquisition either linearizes entirely before commit (old generation) or after commit (new generation); it cannot mix the two.

`GenerationLease` release is synchronous process-local bookkeeping and may safely use `Drop` to decrement the lease count and notify drain. This exception does not change the M7 rule that async durable finalization cannot rely on `Drop`.

## Staged publication primitive

Add a staged swap object/protocol sufficient for R007:

```text
prepared -> staged -> pointer_committed -> accepted/finalized
prepared/staged/pointer_committed -> rolled_back
```

Required operations:

- `stage(expected_generation, prepared_generation)` closes admission and validates active/shutdown/pending-swap state;
- `commit_pointer()` atomically marks old non-accepting and swaps the active Arc while **keeping admission gated**;
- `rollback_pointer()` restores the old slot if acceptance has not completed;
- `accept()` reopens admission, increments publication epoch, transfers candidate ownership, and exposes old slot for retirement;
- abort/rollback reopens admission without a fake publication epoch increment.

The pointer may be staged/committed while admission is closed so R007 can coordinate SQLite/task acceptance without any request observing a half-accepted state.

No method may await while holding the sync publication lock.

## Inference-path integration

Replace `AppState.inference: Arc<InferenceState>` as the request authority for the three M7 inference routes.

For finite requests:

- acquire one `GenerationLease` before any generation-owned admission/routing decision;
- call M7 using `lease.generation().inference`;
- retain the lease until response handoff and terminal ownership registration/completion are finished;
- never reacquire mid-request after an await.

For streaming requests:

- acquire one lease before M7 stream execution;
- move the lease into the spawned Axum body-driving task together with `StreamingExecution`;
- release only after clean terminal, error terminal, disconnect/drop path, or retained terminal registration has converged enough for R004 to drain the supervisor separately;
- a rehash during streaming must not move the stream to the new generation.

R003 may leave non-inference readiness/dashboard authority for R010, but no inference handler may retain the old static `Arc<InferenceState>` path.

## Tests

Required deterministic tests include:

### Publication linearization

- thousands of acquire/publish races with barriers at pre-load, post-load/pre-count, and commit points;
- every lease reports exactly the old or new generation id;
- after commit no newly acquired lease reports old generation;
- rollback admits old generation and does not increment publication epoch;
- shutdown rejects new acquisition/stage.

### Finite generation pinning

- start finite request on generation A, pause provider before response, stage/accept B, finish A request;
- assert provider/config/router facts are exclusively A for that request;
- next request uses B.

### Streaming generation pinning

- start stream on A and receive first chunk;
- publish B while stream remains open;
- stream completes on A with no client-pool/config switch;
- new stream uses B;
- active lease count returns to zero after body task completion/disconnect.

### Gate behavior

- acquisition waits while staged gate is closed and wakes after accept/rollback;
- no lost wakeup under repeated stage/rollback loops;
- timeout/cancellation of an acquisition waiter leaves no lease count or waiter leak.

### M7 safety

- response-start/no-replay remains unchanged;
- dropped finite/stream execution still registers retained terminal work as before;
- no selected claim/reservation leak appears because of lease wrapping.

## Performance/resource review

Measure/inspect the hot path enough to prove it is one Arc load plus a tiny uncontended claim section, not an async manager lock held across the handler. Do not invent a throughput target; M10 owns SBC characterization.

Manager waiter/slot bookkeeping must remain bounded and waiter cancellation must remove itself naturally through Tokio primitives.

## Scope boundaries

R003 must not:

- compute config diffs or read a new config file;
- write config-derived DB state;
- close old generations (R004);
- start task loops;
- change readiness/dashboard authority beyond what inference integration requires;
- implement signal shutdown or CLI/control transport.

## Verification

```text
cargo fmt --all -- --check
cargo clippy --all-targets -- -D warnings
cargo test --all-targets
cargo test --test <R003 manager/lease tests>
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
uv run pytest <R001 lease/publication Python oracle tests> -q --tb=short --maxfail=1
git diff --check
```

## Acceptance criteria

R003 closes only when:

- active publication is atomic and lease claim is linearizable;
- no new old-generation lease appears after commit;
- rollback leaves old state authoritative;
- finite and streaming requests demonstrably remain generation-pinned across a swap;
- streaming body tasks own their lease through terminal/disconnect;
- no static inference authority remains in the production inference handlers;
- full M7 qualification remains green.

## Closure

Write `migration-rs/closure/runtime-lifecycle/003-status.md` and promote R004.
