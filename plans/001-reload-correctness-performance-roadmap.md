# EggPool Reload Correctness, Lifecycle, and Performance Roadmap

Date: 2026-07-19
Status: handoff roadmap
Scope: live rehash correctness, runtime-generation lifecycle, dispatch persistence, control-plane hardening, SQLite contention, diagnostics, CI, and long-running performance stability.

## Executive summary

EggPool’s runtime-generation architecture is directionally sound, but the current live-rehash implementation crosses several ownership boundaries without an atomic commit protocol. Candidate preparation can mutate process-owned services and persisted provider/account state before publication; publication does not consistently update legacy `app.state` mirrors; failed candidate paths do not reliably close resources; runtime lease acquisition can fall back to stale startup objects; and generation retirement is described as asynchronous while the reload call may still wait for long-lived leases to drain.

The same split between startup and reload construction has created concrete performance and behavior drift. The process-level dispatch writer can be started but not selected by the request coordinator, the reload-created coordinator omits dependencies that startup supplies, detailed-span sampling can silently revert to its default after rehash, and persisted health/backoff state is not guaranteed to be hydrated into the new generation. These defects are especially relevant to EggPool’s target deployment profile: long-running processes, SQLite-backed state, high concurrency, streaming responses, and smaller SBC systems where unnecessary lock contention and resource leaks have disproportionate impact.

This roadmap addresses the work in twelve phases. The ordering is intentional:

1. Establish deterministic evidence and invariants.
2. Close immediate admission and stale-runtime hazards.
3. make retirement truly asynchronous.
4. make candidate ownership and cleanup explicit.
5. unify startup and reload construction.
6. implement an all-or-nothing reload transaction.
7. make the runtime manager authoritative for active-generation state.
8. restore dispatch-writer optimization and parity.
9. remove readiness-induced SQLite write contention.
10. harden the Unix control socket and XDG paths.
11. make diagnostics reflect actual state transitions.
12. close the work with CI partitioning, failure injection, soak validation, and measured optimization.

## Target end state

The roadmap is complete when the following invariants hold:

- A rehash either applies completely or leaves runtime, SQLite, process tasks, writers, effective configuration, and diagnostics unchanged.
- Only one reload operation may be admitted at a time; competing requests receive an immediate busy response and never queue behind the active reload.
- Every request holds a valid runtime-generation lease for the complete duration of all asynchronous work that uses generation-owned resources.
- Once a runtime manager exists, request handling never falls back to stale startup-owned coordinators or clients.
- Candidate resources have explicit ownership and are closed exactly once on every failure path.
- Startup and reload use the same production runtime-generation factory and the same dependency wiring.
- Process-owned services are prepared without visible mutation and changed only during a successful, compensatable commit.
- Publishing a generation is a bounded operation; old-generation drainage and teardown occur in tracked background tasks.
- The process dispatch writer is actually used when enabled and remains wired after any number of reloads.
- Readiness polling does not open a SQLite write transaction on each request.
- Control-socket permissions fail closed and runtime paths honor XDG isolation.
- Reload diagnostics report the real failure stage, completion state, active generation, and retirement state.
- CI contains deterministic concurrency and rollback coverage rather than timing-dependent non-strict expected failures.
- Long-running mixed workloads demonstrate stable task, descriptor, client, memory, lock-wait, and dispatch-overhead plateaus.

## Guiding design rules

### Ownership is explicit

Every resource must be classified as one of:

- process-owned: lives for the server process and is shared across generations;
- generation-owned: created for one immutable runtime generation and retired with it;
- transaction-owned: exists only while preparing a candidate and transfers ownership on publication;
- request-owned: bound to a runtime lease and closed or released at request completion.

No object may rely on informal ownership inferred from where it was constructed.

### Prepare before mutate

Reload preparation may validate configuration, construct candidate resources, calculate database deltas, build process-task transitions, and preflight writer settings. It must not alter externally visible state. Mutations are reserved for a narrow commit stage.

### Publication is bounded

The active-generation pointer swap must not wait for active streams to drain. Retirement begins after publication and is tracked independently.

### Startup and reload do not maintain parallel dependency graphs

A single factory must build production generations. Startup may perform bootstrap work around that factory, but it must not independently wire a different coordinator or metrics graph.

### Fail closed on lifecycle uncertainty

When the runtime manager cannot safely provide a lease, the request fails with a bounded 503 response. It does not use a legacy coordinator that may reference a retired generation.

### Measure before optimizing

Performance work begins with stable correctness tests and explicit metrics. Optimization is accepted only when semantics remain equivalent and long-run resource plateaus do not regress.

## Phase map

### Phase 1 — Deterministic correctness baseline

Build a real reload test harness, state snapshots, explicit barriers, and stage-by-stage fault injection. Convert known races and lifecycle defects into strict failing tests before restructuring implementation.

Deliverable: `plans/002-phase-01-correctness-baseline.md`

### Phase 2 — Atomic admission and fail-closed runtime leases

Replace the check-then-lock reload admission race with an atomic claim. Remove request fallback to stale `app.state` coordinators once the runtime manager is installed.

Deliverable: `plans/003-phase-02-admission-and-fail-closed-leases.md`

### Phase 3 — Asynchronous generation retirement

Separate active-generation publication from old-generation drainage. Track retirement tasks, lease counts, deadlines, errors, and shutdown behavior.

Deliverable: `plans/004-phase-03-asynchronous-generation-retirement.md`

### Phase 4 — Candidate resource ownership and cleanup

Introduce a transaction-owned candidate container or `AsyncExitStack`, register every closeable resource immediately, and transfer ownership only on successful publication.

Deliverable: `plans/005-phase-04-candidate-resource-ownership.md`

### Phase 5 — Shared runtime-generation factory

Extract one authoritative production factory used by startup and reload. Restore dependency parity for recorders, writers, sampling configuration, stream diagnostics, and persisted backoffs.

Deliverable: `plans/006-phase-05-shared-runtime-generation-factory.md`

### Phase 6 — Transactional rehash

Implement an explicit reload transaction with prepare, commit, rollback/compensation, cancellation, and shutdown semantics. Prevent mixed runtime/database/process state.

Deliverable: `plans/007-phase-06-transactional-rehash.md`

### Phase 7 — Active-generation state authority

Make `RuntimeManager` the source of truth for generation-owned services. Remove or atomically maintain compatibility mirrors and migrate readiness/dashboard consumers.

Deliverable: `plans/008-phase-07-active-generation-state-authority.md`

### Phase 8 — Dispatch-writer restoration and persistence parity

Ensure the request coordinator actually selects the dispatch writer when enabled, preserve it across reload, and instrument queueing, batching, fallback, and lock-wait behavior.

Deliverable: `plans/009-phase-08-dispatch-writer-restoration.md`

### Phase 9 — Readiness and SQLite contention

Move writable probing to a bounded background task and have `/readyz` consume cached freshness-aware state rather than performing a write transaction per poll.

Deliverable: `plans/010-phase-09-readiness-sqlite-contention.md`

### Phase 10 — Control-plane and XDG hardening

Fail closed on socket-permission errors, validate protocol messages strictly, harden stale-socket handling, and honor `XDG_RUNTIME_DIR` and `XDG_STATE_HOME`.

Deliverable: `plans/011-phase-10-control-plane-and-xdg-hardening.md`

### Phase 11 — Reload diagnostics and operational semantics

Route all reload outcomes through one finalizer, report actual stages, distinguish no-op and ignored-only outcomes, and expose real retirement state.

Deliverable: `plans/012-phase-11-reload-diagnostics.md`

### Phase 12 — CI, soak, performance, and closure

Partition CI, replace expected failures with deterministic tests, run a complete fault matrix, validate long-running resource plateaus, and apply targeted measured optimizations.

Deliverable: `plans/013-phase-12-ci-soak-and-performance-closure.md`

## Dependency graph

- Phase 1 is prerequisite to every implementation phase.
- Phase 2 may land immediately after Phase 1 because it is narrowly scoped and reduces active risk.
- Phase 3 depends on Phase 2’s lease semantics and should land before the larger transaction redesign.
- Phase 4 must precede Phase 6 so failed transaction paths can safely abort candidates.
- Phase 5 must precede Phase 6 so the transaction publishes a complete, parity-checked generation.
- Phase 6 is the central correctness milestone.
- Phase 7 should land with or immediately after Phase 6; otherwise successful commits can still expose stale compatibility state.
- Phase 8 depends on Phase 5 and should use Phase 6 transitions for live process-owned writer changes.
- Phase 9 is mostly independent after Phase 1, but should be validated during Phase 12’s contention tests.
- Phase 10 can proceed in parallel with Phases 4–6 after Phase 1 establishes control-plane tests.
- Phase 11 should be implemented alongside Phase 6 but may be committed separately once transaction stages are stable.
- Phase 12 depends on all preceding phases and is the closure gate.

## Suggested release grouping

### Release A — Immediate safety closure

Phases 1–3:

- deterministic reproduction;
- atomic reload admission;
- fail-closed request leases;
- non-blocking, tracked retirement.

Exit criterion: reload concurrency and old-generation drainage no longer hang or use stale resources.

### Release B — Ownership and construction parity

Phases 4–5:

- complete candidate cleanup;
- shared runtime factory;
- restored dependency and backoff parity.

Exit criterion: failed reloads do not leak resources and successful reloads construct the same service graph as startup.

### Release C — Production-grade transactional rehash

Phases 6–7 and Phase 11:

- explicit transaction;
- process transition plans;
- all-or-nothing state;
- active-generation authority;
- accurate diagnostics.

Exit criterion: live rehash can be treated as production-safe.

### Release D — Performance and control hardening

Phases 8–10:

- dispatch writer active and observable;
- readiness no longer writes per poll;
- control socket and runtime paths hardened.

Exit criterion: intended persistence optimization is active and routine operational traffic does not create avoidable SQLite contention.

### Release E — Closure

Phase 12:

- CI matrix and partitioning;
- complete fault injection;
- long-running soak and plateau evidence;
- targeted performance polish.

Exit criterion: all roadmap invariants are continuously enforced.

## Global non-goals

- Do not rewrite the provider-routing algorithm except where required to preserve generation correctness.
- Do not replace SQLite as part of this roadmap.
- Do not introduce Rust extensions before Python-level lifecycle and batching defects are fixed and measured.
- Do not make every configuration field live-reloadable; restart-required fields may remain explicit.
- Do not preserve legacy `app.state` fallback behavior merely for compatibility when it violates generation ownership.
- Do not accept time-based sleeps as proof of concurrency correctness.
- Do not broaden the control protocol beyond the commands required for this line of work.

## Global evidence requirements

Every implementation phase must produce:

- tests that fail against the pre-phase implementation and pass after the change;
- a focused test command suitable for local handoff;
- full-suite results;
- relevant type-check and lint results;
- a short implementation note identifying changed ownership or lifecycle contracts;
- before/after metrics for phases making performance claims;
- no new non-strict `xfail`, broad skip, or swallowed task exception covering a roadmap invariant.

## Final definition of done

The line of work is closed only when:

1. Reload admission is atomic and deterministic.
2. Rehash is all-or-nothing across runtime, database, process tasks, shared writers, and observable configuration.
3. Candidate and retired-generation resources close exactly once.
4. Startup and reload share one runtime-generation factory.
5. Requests never fall back to stale generation-owned services.
6. Generation retirement is asynchronous, tracked, bounded, and shutdown-safe.
7. Persisted health/backoff state and metrics configuration survive reload.
8. Dispatch persistence uses the configured microbatch writer before and after reload.
9. Readiness polling performs no write transaction in the request path.
10. The control plane fails closed and supports isolated XDG runtime/state paths.
11. Diagnostics accurately represent every terminal reload outcome.
12. CI and soak evidence demonstrate stable long-running memory, task, descriptor, connection, lock-wait, and dispatch-overhead behavior.