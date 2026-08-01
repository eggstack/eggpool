# Durable Convergence, Exact-Version Update, and SBC Hot-Path Roadmap

Date: 2026-07-31
Last reviewed: 2026-08-01
Status: completed
Plan: 058
Planning baseline: `daef79cd98f23b11cc8d5a254c28abf64df1791a`
Implementation review baseline: `d504005e46625bb5d8df72f5306d6eafb11d43b8`

Related completed and corrective work:

- `plans/045-upstream-streaming-hardening-hotpath-roadmap.md`
- `plans/054-test-suite-and-verification-reduction.md`
- `plans/055-terminal-stream-lifecycle-corrective-pass.md`
- `plans/056-retained-cleanup-convergence-closure.md`
- `plans/057-retained-cleanup-final-closure.md`

Implementation plans:

- `plans/059-dispatch-persistence-contract-and-writer-boundary.md`
- `plans/060-database-recovery-admission-and-ambiguity.md`
- `plans/061-terminal-convergence-and-reconciliation.md`
- `plans/062-stale-runtime-accounting-closure.md`
- `plans/063-exact-version-update-command.md`
- `plans/064-quota-and-sqlite-hotpath-reduction.md`
- `plans/065-terminal-recovery-and-small-regression-closure.md`
- `plans/066-terminal-runtime-ownership-and-supervisor-closure.md`

## Purpose

Close the remaining correctness defects found in the current request, persistence, recovery, and terminal-lifecycle paths, add exact-version support to `eggpool update`, and remove the most credible long-running hot-path costs without rebuilding EggPool as a production-grade distributed control plane.

The repository has already made substantial progress on provider client reuse, request-body reuse, bounded SSE framing, routing-plan reuse, retained cleanup, and reduced CI. The remaining work is narrower but important: several failure paths can still report success before durable state exists, recovery can reopen traffic before reconciliation is complete, and terminal cleanup has multiple partially overlapping ownership mechanisms with inconsistent identity and result semantics.

The design center remains a private EggPool deployment on one SBC or small LAN host:

- one process supervisor and one canonical event-loop thread;
- one SQLite database using WAL;
- a modest number of provider accounts and concurrent streams;
- no hostile public multi-tenant control plane;
- correctness under ordinary upstream, disk, cancellation, and restart failures;
- fast local iteration with a small smoke gate.

## Confirmed findings

### Correctness-critical

1. Dispatch microbatch persistence can roll back and still return nominal result objects with empty request/reservation IDs and attempt ID zero. The writer and coordinator currently treat those values as successful persistence.
2. Ambiguous database-operation ownership is stored in one process-global mutable slot. Concurrent tasks can overwrite or clear another transaction's descriptor.
3. Database recovery admits reads and writes before schema verification, writable probing, and ambiguous-operation reconciliation finish.
4. A failed recovery attempt can leave the replacement connection open and admitted even though the recovery controller reports failed closed.
5. Finalization reconciliation uses request and attempt identities inconsistently and does not use the same status vocabulary as the durable writers.
6. Ambiguous-operation reconciliation drains records before proving convergence and treats conflicts or unknown strategies as resolved.
7. The finalization supervisor records retry intent but does not schedule retries. Saturation can also return detached, untracked work.
8. The older finalization retry queue treats an already-terminal idempotent result as failure and cannot distinguish durable convergence from missing runtime cleanup.
9. The stale-request safety sweep under-decrements active counts when several stale requests belong to one account and can leak zero-cost reservations that still own request/token pressure.
10. Dispatch-writer cross-loop support is implied but not correctly implemented. The runtime is canonically single-loop, so the boundary should be made explicit rather than generalized.

### CLI behavior

11. `eggpool update` only targets the latest PyPI release. It cannot request an exact published version, perform a deliberate downgrade, or report that a requested version does not exist.
12. Bare `eggpool update` must retain its current fresh PyPI lookup and latest-version behavior.

### Performance and long-running behavior

13. Quota rolling windows scan and copy retained observations on every update and read, making cumulative work grow with process age.
14. Persisted rolling-usage snapshots need explicit aging semantics during a continuously running generation.
15. Request finalization writes a large set of canonical and diagnostic columns in one correctness-critical transaction.
16. Routing trace batch persistence loops over individual inserts and broadly suppresses database errors.
17. Dispatch microbatching should be retained only if a small local comparison shows benefit at realistic SBC concurrency; it must not accumulate more batching machinery.

## Post-implementation review

The implementation through `d504005e46625bb5d8df72f5306d6eafb11d43b8` closed the main dispatch, recovery, durable-finalization, stale-accounting, exact-update, quota, and SQLite objectives. Plan 065 also removed the legacy production retry queue, retired exhausted jobs, rejected detached saturation work, introduced truthful durable convergence fields, aligned database `READY` state with admission, corrected the quota late-event anchor, and restored bare update output.

A final bounded runtime-ownership gap remains:

1. production retained jobs are registered without `AttemptRuntimeLease` ownership;
2. retryable quota/router/usage/health cleanup remains inside `RequestFinalizer` and is skipped after an already-terminal retry;
3. runtime result fields can report durable reservation facts as in-memory cleanup;
4. retry age is not rechecked when a due heap entry executes;
5. coordinator capacity rejection lacks explicit pre/post-handoff semantics;
6. operator documentation references a finalization-supervisor runtime snapshot that is not exposed;
7. roadmap and Plan 065 status/checklist metadata require final reconciliation.

Plan 066 was the sole corrective closure plan for these residuals. It extended
the existing lease and supervisor without adding another queue, lifecycle
framework, migration, or verification system.

## Governing constraints

1. **No production-control-plane expansion.** Do not add distributed consensus, external queues, workflow engines, or multi-node recovery semantics.
2. **One durable identity per operation.** Request, attempt, and reservation identities must remain explicit and must not be inferred from unrelated fields.
3. **Success means convergence.** Empty identifiers, unresolved ambiguity, queued-but-unowned finalization, or durable/runtime disagreement are not successful outcomes.
4. **Recovery remains closed until complete.** A replacement database connection is not ready merely because it opened.
5. **Use existing ownership structures.** Prefer tightening `Database`, repositories, `RequestFinalizationSupervisor`, finalizers, and the coordinator over adding parallel systems.
6. **Canonical single-loop runtime.** Do not build general cross-event-loop queueing for a deployment model that intentionally uses one loop.
7. **No mandatory schema migration unless unavoidable.** The planned corrections should use existing tables and process-owned bounded state. A migration requires explicit proof that the invariant cannot be represented otherwise.
8. **No new runtime dependency for version parsing.** Reuse the project's existing simple PEP 440 subset and PyPI response handling.
9. **No test-suite expansion by plan number.** Tests belong in existing capability-based files and remain after the plan is closed.
10. **No new CI job, matrix, coverage gate, benchmark gate, soak gate, or evidence bundle.**

## Phase sequence

### Plan 059 — Dispatch Persistence Contract and Writer Boundary

Make persistence failure exceptional, validate persisted identifiers before publication, and explicitly enforce the canonical single event loop. This phase closes the path where a rolled-back batch can still reach upstream dispatch.

### Plan 060 — Database Recovery Admission and Ambiguity

Move ambiguous-operation metadata into transaction ownership, keep admission closed through verification and reconciliation, and retain unresolved records rather than declaring recovery ready.

### Plan 061 — Terminal Convergence and Reconciliation

Give request and attempt finalization distinct durable identities, unify their result semantics, make the existing supervisor perform bounded retries, and route recovery reconciliation through the same convergence rules.

### Plan 062 — Stale Runtime Accounting Closure

Correct stale-request aggregation and zero-cost reservation release. Use exact per-account totals and avoid replaying runtime release after durable convergence.

### Plan 063 — Exact-Version Update Command

Add one optional version argument to `eggpool update`, normalize a leading `v`, check the exact PyPI release, install the exact target when present, allow deliberate downgrade, and preserve bare-command latest behavior.

### Plan 064 — Quota and SQLite Hot-Path Reduction

Convert quota pruning to an amortized constant-time normal path, prove rolling snapshots age correctly, batch trace inserts, and reduce finalization write cost only where a compact local measurement proves it material.

### Plan 065 — Terminal Ownership, Recovery State, and Small Regression Closure

Retire exhausted jobs, reject saturation before detached ownership, return explicit durable convergence facts, remove the legacy production retry queue, align recovery state with admission, correct quota late-event pruning, and restore bare update output.

### Plan 066 — Terminal Runtime Ownership and Supervisor Closure

Carry explicit runtime publication ownership into the retained job, make post-commit runtime convergence resumable and exactly-once, enforce retry age at execution, define capacity rejection semantics, expose supervisor diagnostics, and reconcile closure metadata.

## Dependency order

```text
059 dispatch persistence --------+
060 database recovery -----------+--> 061 terminal convergence --> 062 stale accounting --+
                                 |                                                        |
063 exact-version update --------+--------------------------------------------------------+--> 065 durable closure --> 066 runtime closure
064 quota/SQLite hot path -------+--------------------------------------------------------+
```

Plan 061 depends on the transaction and recovery semantics from Plan 060. Plan 062 consumes the durable finalization and accounting semantics. Plans 063 and 064 are otherwise independent. Plan 065 closed the bounded durable/recovery regressions. Plan 066 closes the remaining in-process runtime ownership and operator-diagnostic gap without reopening completed dispatch, recovery, update, or hot-path architecture.

## Cross-phase invariants

- No upstream request is sent until non-empty durable request and reservation IDs and a positive attempt ID exist.
- A failed batch resolves every affected caller with an exception; no synthetic success object represents rollback.
- Ambiguous-operation metadata belongs to the transaction holding the database lock.
- Reads and writes remain rejected while recovery is opening, verifying, probing, or reconciling.
- Recovery cannot report ready while any correctness-critical ambiguity is unresolved.
- Controller state, database lifecycle state, and admission flags agree after recovery.
- Request finalization and attempt finalization use distinct strategy names and identity fields.
- An already-terminal request is fully converged only when required attempt, durable reservation, and runtime ownership are also converged.
- Every production selected terminal job carries explicit runtime ownership derived from publication facts.
- Durable reservation release and in-memory quota reservation removal are separate facts.
- Runtime quota, active-count, usage, health, account-state, and probe convergence occur at most once per owned component.
- One supervisor owns automatic in-process terminal retry; no retry begins after the absolute configured retry age.
- Capacity rejection is fail-closed and observable and never creates detached work.
- One stale request contributes one active-request decrement even when several stale requests share an account.
- A reservation with zero monetary cost can still own request and token pressure and must be released.
- `eggpool update` with no argument still targets the latest live PyPI version and reports current/latest versions before its conclusion.
- `eggpool update vX.Y.Z` and `eggpool update X.Y.Z` resolve to the same exact target.
- Ordered quota-window work remains amortized constant-time; the rare rebuild expires against the newest observation timestamp.
- Performance changes remove deterministic work or are justified by one compact local measurement; no timing percentage becomes a CI gate.

## Verification budget

The implementation plans define focused cases, but the aggregate rules are:

- modify existing unit and smoke files rather than creating plan-numbered suites;
- normally add no more than four to six focused cases per implementation plan;
- use parameterization for equivalent input shapes;
- use one real SQLite transaction test where mocking could hide ownership or row-shape behavior;
- use one CLI runner file for exact-version/latest behavior and one update-checker file for HTTP parsing;
- do not duplicate every install method through full subprocess execution; assert command construction and run one representative invocation path;
- no live provider credentials;
- no mandatory PyPI network access in tests;
- no repeated cancellation campaigns, soak loops, or fault matrices;
- ordinary CI remains the existing single smoke job;
- local performance checks are diagnostic scripts or direct commands, not retained CI infrastructure.

## Roadmap acceptance criteria

- [x] A rolled-back dispatch batch cannot publish runtime ownership or send an upstream request.
- [x] Persisted dispatch results reject empty IDs and non-positive attempt IDs.
- [x] Ambiguous-operation descriptors cannot be overwritten by another waiting transaction.
- [x] Database admission remains closed until verification, probing, and reconciliation all succeed.
- [x] Failed recovery closes the replacement connection and leaves the admission event clear.
- [x] Unresolved or conflicting ambiguous operations remain visible and prevent a false ready state.
- [x] Request and attempt finalization reconcile against the correct durable rows and status vocabulary.
- [x] Durable finalization reports actual request, attempt, and reservation convergence facts.
- [x] The finalization supervisor schedules bounded retries, retires exhausted work, and rejects capacity before returning detached work.
- [x] Every production selected terminal job carries explicit runtime ownership.
- [x] Partial post-commit runtime failure resumes without replaying durable or completed runtime components.
- [x] Durable reservation release is distinguished from live quota/router/health convergence in results.
- [x] No timer-driven retry begins after the configured absolute retry age.
- [x] Coordinator capacity rejection has explicit pre-handoff and post-handoff semantics.
- [x] Runtime metrics expose the active finalization supervisor's bounded snapshot.
- [x] The legacy finalization queue and drain task no longer participate in production ownership.
- [x] Successful recovery leaves controller state, database lifecycle state, and admission flags coherently `READY`.
- [x] Stale cleanup decrements the exact number of active requests and releases zero-cost request/token reservations.
- [x] Bare `eggpool update` preserves latest-update behavior and established current/latest output.
- [x] Exact-version update accepts with or without a leading `v`, verifies release existence, supports newer or older targets, and reports a clear error for a missing release.
- [x] Quota-window update cost does not grow linearly with retained observation count on the ordered-time path.
- [x] Out-of-order quota observations expire against the newest known timestamp.
- [x] Rolling snapshots demonstrably expire old usage during long-lived operation.
- [x] Routing traces use a true batch write and unexpected database failures are not silently suppressed.
- [x] No new CI job, matrix, coverage threshold, soak gate, evidence format, workflow engine, durable work queue, or generalized cross-loop runtime is introduced.

## Rejection conditions

Do not close this roadmap if:

- any failure path still fabricates successful persistence identifiers;
- recovery sets the ready/admission state before reconciliation completes or reports lifecycle state inconsistent with admission;
- conflicts are counted as successful reconciliation;
- a production retained terminal job has no explicit runtime ownership;
- a retry after durable commit can skip unfinished quota, active-count, usage, health, account-state, or probe convergence;
- runtime completion is inferred from durable reservation state;
- a retry begins after the absolute retry deadline;
- saturation creates detached work or escapes without explicit coordinator semantics;
- operator documentation references supervisor diagnostics not exposed by the runtime API;
- exact-version update silently substitutes latest for a missing requested release;
- explicit downgrade is blocked by a latest-only comparison;
- source-checkout limitations are hidden rather than reported clearly;
- an expired late quota observation remains counted relative to a newer observation;
- a performance phase introduces a new framework without measured need;
- test-support code or CI complexity grows materially relative to the runtime fix.

## Definition of done

This roadmap is complete when Plans 059–065 remain intact, Plan 066 closes the bounded runtime ownership, deadline, capacity, diagnostic, and metadata residuals, durable persistence, recovery, finalization, runtime accounting, and stale repair agree on explicit identities and convergence, exact-version and bare latest updates behave as documented, quota and trace hot paths have bounded normal-path cost, focused regressions and the existing smoke suite pass, and the repository remains simpler to iterate on than a production-grade service with equivalent failure machinery.

## Current implementation state

Plans 059–064 and most of Plan 065 are implemented. Durable dispatch,
recovery admission, durable terminal convergence, stale accounting,
exact-version updates, quota-window maintenance, and trace batching are in
place. The legacy production retry queue is removed and the supervisor now
performs bounded retries and clean exhaustion.

Final closure remains pending on Plan 066 because the production retained job
does not yet carry resumable runtime ownership, retry execution does not yet
enforce the absolute age at the due boundary, capacity rejection lacks
explicit coordinator semantics, and runtime metrics do not yet expose the
supervisor snapshot. No additional roadmap or verification framework is
warranted.
