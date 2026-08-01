# Durable Convergence, Exact-Version Update, and SBC Hot-Path Roadmap

Date: 2026-07-31
Last reviewed: 2026-08-01
Status: corrective closure pending
Plan: 058
Planning baseline: `daef79cd98f23b11cc8d5a254c28abf64df1791a`
Implementation review baseline: `94c6555eba6f2ebfcc86712b5aeabb041825fade`

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

The implementation commits through `94c6555eba6f2ebfcc86712b5aeabb041825fade` landed the main roadmap architecture without expanding CI or persistence scope. Dispatch persistence, recovery admission, stale accounting, exact-version targeting, ordered quota pruning, persisted-window refresh, and trace batching are substantially in place.

The review found a narrow closure set that prevents final completion:

1. retry-age exhaustion leaves a failed job in active capacity and retains operational references;
2. timer-driven retry counts are not recorded and capacity saturation still returns a detached rejected job;
3. the durable finalizer still returns one boolean while the job layer infers broader convergence facts;
4. the legacy finalization queue remains constructed and periodically drained despite the supervisor being the intended sole retry owner;
5. successful recovery admits traffic without transitioning the `Database` lifecycle state from `RECOVERING` to `READY`;
6. the quota out-of-order slow path prunes against the late timestamp instead of the newest timestamp;
7. bare update no longer prints current/latest versions before `Already up to date.`;
8. roadmap and predecessor closure metadata need correction after the runtime fixes land.

Plan 065 is the sole corrective closure plan for these residuals. It must remain narrow; another roadmap, queue, lifecycle framework, or verification system is not warranted.

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

Retire exhausted terminal jobs without leaking active capacity, reject saturation before ownership transfer, return truthful durable convergence facts, remove the legacy queue from production ownership, align database lifecycle state with recovery admission, correct the quota late-event anchor, restore bare update output, and close roadmap metadata.

## Dependency order

```text
059 dispatch persistence --------+
060 database recovery -----------+--> 061 terminal convergence --> 062 stale accounting --+
                                 |                                                        |
063 exact-version update --------+--------------------------------------------------------+--> 065 closure
064 quota/SQLite hot path -------+--------------------------------------------------------+
```

Plan 061 depends on the transaction and recovery semantics from Plan 060. Plan 062 consumes the finalization and accounting semantics. Plans 063 and 064 are otherwise independent. Plan 065 reviews and closes the bounded residuals across 060, 061, 063, and 064; it does not reopen completed dispatch or stale-accounting architecture.

## Cross-phase invariants

- No upstream request is sent until non-empty durable request and reservation IDs and a positive attempt ID exist.
- A failed batch resolves every affected caller with an exception; no synthetic success object represents rollback.
- Ambiguous-operation metadata belongs to the transaction holding the database lock.
- Reads and writes remain rejected while recovery is opening, verifying, probing, or reconciling.
- Recovery cannot report ready while any correctness-critical ambiguity is unresolved.
- Controller state, database lifecycle state, and admission flags agree after recovery.
- Request finalization and attempt finalization use distinct strategy names and identity fields.
- An already-terminal durable row is a converged result only when the required attempt/reservation state is also terminal.
- One supervisor owns automatic in-process terminal retry; exhausted jobs do not retain active capacity.
- Runtime quota, active-count, health, and probe release occur at most once per owned component.
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
- [ ] The existing finalization supervisor performs bounded retries, retires exhausted work, and never returns detached untracked work.
- [ ] Durable finalization reports truthful request/attempt/reservation convergence and outstanding runtime ownership can still be repaired exactly once.
- [ ] Production has one automatic in-process terminal retry owner; the legacy queue/drain is not active.
- [ ] Successful recovery leaves controller state, database lifecycle state, and admission flags coherently `READY`.
- [x] Stale cleanup decrements the exact number of active requests and releases zero-cost request/token reservations.
- [ ] Bare `eggpool update` preserves latest-update behavior and established current/latest output.
- [x] Exact-version update accepts with or without a leading `v`, verifies release existence, supports newer or older targets, and reports a clear error for a missing release.
- [x] Quota-window update cost does not grow linearly with retained observation count on the ordered-time path.
- [ ] Out-of-order quota observations expire against the newest known timestamp.
- [x] Rolling snapshots demonstrably expire old usage during long-lived operation.
- [x] Routing traces use a true batch write and unexpected database failures are not silently suppressed.
- [x] No new CI job, matrix, coverage threshold, soak gate, evidence format, workflow engine, durable work queue, or generalized cross-loop runtime is introduced.

## Rejection conditions

Do not close this roadmap if:

- any failure path still fabricates successful persistence identifiers;
- recovery sets the ready/admission state before reconciliation completes or reports a lifecycle state inconsistent with admission;
- conflicts are counted as successful reconciliation;
- terminal retry ownership remains split across active mechanisms;
- exhausted or saturated jobs leave detached/untracked terminal ownership;
- a boolean still represents multiple durable convergence meanings;
- exact-version update silently substitutes latest for a missing requested release;
- explicit downgrade is blocked by a latest-only comparison;
- source-checkout limitations are hidden rather than reported clearly;
- an expired late quota observation remains counted relative to a newer observation;
- a performance phase introduces a new framework without measured need;
- test-support code or CI complexity grows materially relative to the runtime fix.

## Definition of done

This roadmap is complete when Plans 059–064 remain intact, Plan 065 closes the bounded ownership/state/output regressions, durable persistence, recovery, finalization, and stale-accounting paths agree on explicit identities and convergence, exact-version and bare latest updates behave as documented, quota and trace hot paths have bounded normal-path cost, focused regressions and the existing smoke suite pass, and the repository remains simpler to iterate on than a production-grade service with equivalent failure machinery.