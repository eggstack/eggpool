# Plan 182 — Runtime Generation and Task-Lifecycle Decomposition

Date: 2026-09-12
Status: ready for handoff
Parent roadmap: `plans/178-rust-maintenance-consolidation-roadmap.md`
Planning baseline: `78a4da64de3c94a9f5fe29e05e6bdf40402bc16b`
Priority: P1/P2 maintainability / lifecycle correctness
Execution target: GPT-5.6 Luna/Sol or comparable implementation model

## Purpose

Decompose `runtime_lifecycle.rs` and its interaction with `task_supervisor.rs` along the lifecycle boundaries that already exist in the implementation, while preserving the exact generation, lease, publication, retirement, recovery, diagnostics, and background-task state machines.

This phase is intentionally structural. Runtime generation behavior is security/reliability-sensitive and should not be redesigned merely because the current implementation is large.

## Current-state findings

`runtime_lifecycle.rs` is roughly 89 KiB and currently owns several distinct but related concerns:

- process-owned runtime resources (`ProcessRuntime`);
- startup crash reconciliation;
- generation construction and build failures;
- immutable runtime generations;
- generation slots and explicit request leases;
- active-generation publication through `ArcSwap`;
- bounded retiring-generation queues;
- reload/publication/shutdown diagnostics;
- task-supervisor integration and task-spec transitions;
- generation/provider/finalization close behavior.

`task_supervisor.rs` is roughly 48 KiB and correctly owns process/generation background-task handles, callback registration, task specifications/diffs, execution state, and bounded shutdown.

The ownership model is good: the process owns the active generation manager, request work holds leases across awaits, and retirement cannot destroy resources still pinned by in-flight work. This plan must make that easier to navigate without changing it.

## Target module shape

Prefer a package structure that follows state-machine ownership. One reasonable target is:

```text
runtime_lifecycle/
  mod.rs             # public re-exports and compact ownership overview
  process.rs         # ProcessRuntime and process-lifetime shared resources
  generation.rs      # RuntimeGeneration / generation construction
  lease.rs           # generation slots, states, GenerationLease
  manager.rs         # active publication, ArcSwap, retirement queue
  recovery.rs        # startup reconciliation
  diagnostics.rs     # bounded secret-free lifecycle snapshots/counters
  shutdown.rs        # close/retire/shutdown coordination if naturally separable
```

Exact filenames should follow the actual code after inventory. Avoid modules that contain only a trivial type or forwarder.

`task_supervisor.rs` may remain a standalone module if its current cohesion is strong. Split it only where there is a clear internal seam such as task definitions/spec compilation versus running-handle supervision. Do not move task ownership into `server.rs` or `reload.rs`.

## Governing constraints

1. Preserve `ArcSwap` active-generation publication semantics.
2. Preserve the rule that a `GenerationLease` pins the exact generation across every await until its owning request/body task is finished.
3. Preserve bounded retirement (`MAX_RETIRING_GENERATIONS`) and the existing close timeout/failure diagnostics.
4. Preserve startup crash reconciliation order, bounds, convergence semantics, and durable finalization/accounting repair.
5. Preserve process-lifetime shared objects such as database handles, model-router affinity, wire-profile resolver, metrics coalescer, update checker, reload lock, and task supervisor.
6. Preserve the distinction between process-owned and generation-owned background tasks.
7. Preserve reload serialization and diagnostics ownership established by `reload.rs`/Plan 181.
8. Preserve graceful shutdown ordering and forced-shutdown diagnostics.
9. Do not add actors, channels, a generic resource graph, or a new runtime framework.
10. Do not introduce more `Arc<Mutex<...>>` solely to make extraction easier. Existing synchronization ownership should move with the state it protects.
11. Keep diagnostics bounded and secret-free; do not serialize `Config`, credentials, proxy URLs, prompt data, or provider response bodies into lifecycle snapshots.
12. Do not change task intervals or enable/disable policy in a structural commit.

## Workstream A — Inventory lifecycle state and lock ownership

Before moving code, produce an ephemeral map of:

- every atomic, mutex, notify, async mutex, `ArcSwap`, and join handle in lifecycle/supervisor code;
- the state it protects;
- who mutates it;
- whether it is process-lifetime or generation-lifetime;
- shutdown/retirement ordering dependencies.

Use this map to keep synchronization state adjacent to its owner after extraction. Do not commit a permanent synchronization diagram unless it materially improves `architecture/` documentation.

## Workstream B — Extract diagnostics and startup recovery first

Move the lowest-risk cohesive pieces before touching publication:

- secret-free runtime diagnostic snapshot types/state helpers;
- startup recovery report/error and reconciliation loop.

Keep the public types/re-exports stable so server/runtime-status code does not need semantic changes.

Verify startup recovery remains a pre-admission operation and that truncation/convergence bounds are unchanged.

## Workstream C — Separate process-lifetime resource ownership

Move `ProcessRuntime` and its construction/accessors into a dedicated internal module.

Keep explicit ownership of:

- database;
- semantic-routing affinity;
- wire resolver;
- config path;
- task supervisor;
- metrics write coalescer;
- update checker;
- reload serialization state;
- startup recovery report;
- lifecycle diagnostics.

Do not turn this into a generic dependency container. `ProcessRuntime` is a concrete process-resource owner and should remain one.

## Workstream D — Separate generation construction from publication

Move generation-build code/errors and immutable generation resources away from manager publication code.

Generation construction should remain side-effect-bounded and unable to replace the active generation on failure. Provider pool creation, profile/model-router compilation, DB preconditions, inference-state construction, and candidate task specs should all finish or fail before publication authority is invoked.

Retain cleanup of partially constructed resources on failure.

## Workstream E — Isolate lease/slot and publication/retirement mechanics

Keep the core state machine explicit:

```text
candidate built
    -> publish active slot
    -> prior active becomes retiring
    -> existing leases continue
    -> terminal references/finalizers drain
    -> provider/generation resources close
    -> retiring slot removed
```

Move slot state, lease counters, terminal-reference ownership, retirement readiness, and manager queue/publication code into cohesive internal modules.

Avoid hidden callbacks between them. Prefer explicit methods whose names correspond to state transitions already present in tests.

## Workstream F — Keep task supervision a separate owner

Audit the broad re-export currently made from `runtime_lifecycle` for `task_supervisor` types. Retain compatibility where tests/callers depend on it, but reduce unnecessary coupling where a caller can import task-supervisor types from their real owner.

If `task_supervisor.rs` is split, a reasonable internal separation is:

- capability/spec compilation/diff;
- callback registry;
- running task handle/state supervision;
- bounded shutdown/reporting.

Do not create a task trait hierarchy if the current callback/spec representation is sufficient.

## Workstream G — Update architecture and module-boundary tests

Update current architecture docs to describe:

- process resources;
- immutable generations;
- request leases;
- atomic publication;
- bounded retirement;
- process/generation task ownership.

Adjust `coordinator_boundaries.rs` or lifecycle-focused tests only where module paths changed. Do not weaken behavioral assertions to accommodate the refactor.

## Focused verification

Run the complete lifecycle regression family because this refactor changes file/module boundaries around the state machine:

```bash
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r002 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r003 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r004 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r005 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r006 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r007 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r010 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r011 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r012 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r013 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
```

Then run strict Clippy and the full workspace/tooling verification baseline from Plan 178.

## Acceptance criteria

- Runtime lifecycle code is split along process, generation, lease/slot, publication/retirement, recovery, and diagnostic ownership rather than arbitrary size targets.
- `ProcessRuntime` remains a concrete process-resource owner, not a generic service locator.
- candidate generation construction cannot mutate active authority before successful publication.
- lease/retirement semantics and all lifecycle R002–R013 regressions are unchanged.
- task supervisor remains the sole owner of supervised background handles and bounded task shutdown.
- diagnostics remain bounded/secret-free.
- no new concurrency framework or dependency is introduced.
- full repository qualification remains green.

## Handoff note

Move the state machine in slices and run the relevant lifecycle tests after each slice. A compile-only mechanical move is not enough: publication, lease, retirement, and shutdown behavior are the actual contract.