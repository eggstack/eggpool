# Reload Atomicity Closure Corrective Pass

Date: 2026-07-23
Status: implementation handoff

Depends on:

- `plans/015-reload-atomicity-final-closure.md`
- `plans/016-reload-atomicity-corrective-closure.md`
- `plans/017-reload-atomicity-final-corrective-pass.md`

Implementation baseline:

- `5665444f7cb6c0ca68f2ccdfa9e39fbbd95b4e55`
- `dd540b1d0377cfea1c6821e03debe26a8b9ce8d3`

## Objective

Close the remaining production correctness defects in the live reload transaction after the Plan 017 implementation.

The Plan 017 implementation successfully replaced the lease event protocol with condition-based admission, added explicit acceptance state, introduced structured rollback diagnostics, and added a real commit-call wrapper. Those changes are retained.

This pass is required because the current implementation still permits several invalid states:

1. A partially applied process-transition prefix is attached to `ProcessTransitionApplyError`, but the production caller loses that result and passes `None` to cleanup.
2. An exception from `on_publish_complete()` occurs after SQLite and runtime publication are accepted but is still handled by the preacceptance abort block.
3. Post-acceptance cancellation is later marked `ABORTED`, can omit compatibility mirroring and transition finalization, and can orphan the old generation without a retirement owner.
4. Retirement-scheduling failure records a degraded success but does not retain an executable retry owner for the old generation.
5. An indeterminate SQLite commit failure records `connection_invalidated=True` without actually invalidating or quarantining the connection.
6. Candidate ownership-state comparisons use uppercase strings against lowercase enum values.
7. Defensive lease-gate repair increments `publication_epoch` when no publication occurred and can open admission while a staged swap remains unresolved.
8. Several tests named as production-boundary tests inject before the boundary they claim to verify or manually toggle bookkeeping fields without exercising production execution.
9. There is no Plan 018 exact-head CI evidence job.

This plan is complete only when accepted reloads cannot enter rollback, old generations always retain a finalization/retirement owner, partial transitions are restored through the production path, indeterminate database connections cannot be reused, and tests prove the exact boundaries rather than approximate them.

---

# Scope

## In scope

- production ownership of `TransitionApplyResult`;
- partial transition rollback and partial rollback retry semantics;
- the exact accepted-reload boundary;
- separation of preacceptance cleanup from post-acceptance finalization;
- observer callback behavior after acceptance;
- post-acceptance cancellation handling;
- an executable, idempotent accepted-finalization job;
- retained ownership of committed pending swaps and old-generation retirement;
- retry behavior before the next reload and during shutdown;
- actual database connection invalidation after indeterminate commit outcomes;
- truthful transaction, ownership, publication, and finalization diagnostics;
- strict candidate ownership-state checks;
- safe defensive lease-gate repair;
- replacement of overstated tests with production-path barrier tests;
- a dedicated Plan 018 Python 3.11/3.12 CI job and exact-head evidence.

## Explicit non-goals

Do not:

- redesign config validation, semantic diffing, or reload policy classification;
- replace the `RuntimeManager`, `RuntimeGeneration`, or generation-lease architecture;
- replace SQLite or aiosqlite;
- add distributed transactions or cross-process reload coordination;
- make `app.state` authoritative for request dispatch;
- modify provider routing, transcoding, compression, accounting, or dashboard behavior;
- add general crash recovery beyond the explicitly documented in-process and SQLite guarantees;
- silently discard accepted-finalization work to allow a later reload;
- use broad retries around the complete reload transaction;
- hide degraded outcomes behind an unconditional `ok=True` result without diagnostics;
- retain tests whose names claim a boundary they do not exercise.

This is a closure correction over Plans 015–017, not another reload architecture rewrite.

---

# Required invariants

## Process-transition invariants

1. The production reload owner constructs and stores the exact `TransitionApplyResult` before the first transition can mutate state.
2. If transition `N` fails, transitions `0..N-1` remain reachable through the production exception path.
3. The shared preacceptance cleanup owner receives that exact result object.
4. Applied transitions are rolled back in reverse order.
5. The failed transition and transitions after it are not reported as applied.
6. A rollback failure does not erase the primary apply failure.
7. A rollback failure does not mark the complete result as successfully rolled back.
8. Retrying rollback invokes only transitions that remain unrestored.
9. Transition finalization failures remain visible and retryable; they are not swallowed and then marked complete.
10. No process-transition rollback executes after reload acceptance.

## Acceptance invariants

11. Reload acceptance occurs only after confirmed SQLite commit success and committed runtime swap visibility.
12. `txn.reload_accepted` is the sole branch discriminator between rollback-capable and rollback-forbidden handling.
13. Once `txn.reload_accepted` becomes true, no path may invoke:
    - `pending_swap.rollback()`;
    - `transition_result.rollback_applied()`;
    - `candidate.abort()`;
    - old-generation restoration;
    - `txn.mark_aborted()`.
14. No awaited observer, logging, event recording, mirroring, transition finalization, or retirement operation remains inside the preacceptance exception block after acceptance is set.
15. Accepted reloads remain accepted when finalization fails or the caller is cancelled.
16. Accepted reload diagnostics distinguish complete finalization from pending/degraded finalization.

## Accepted-finalization invariants

17. Every accepted reload creates one process-owned finalization job before the first post-acceptance await.
18. The job retains strong references to all state needed for retry:
    - transaction;
    - candidate container;
    - committed pending swap;
    - transition result;
    - published generation;
    - old generation ID and slot ownership through the pending swap;
    - app compatibility mirror target;
    - observer/reporting metadata.
19. Finalization steps are idempotent and execute in a declared order.
20. Failure at one step leaves the job registered at that exact step.
21. A later retry resumes from the first incomplete step without repeating completed destructive actions.
22. A committed pending swap is not force-cleared while finalization or retirement is unresolved.
23. A new reload is not admitted while an accepted-finalization job remains unresolved, unless the job is first completed by a bounded retry.
24. Shutdown attempts bounded completion of all accepted-finalization jobs before retiring the active generation.
25. If bounded retry cannot complete, diagnostics preserve the job and report operator-visible degraded state.
26. Old-generation retirement is either scheduled exactly once or remains owned by a registered retry job.
27. No old generation is orphaned by cancellation, observer failure, mirror failure, transition-finalization failure, or retirement-scheduling failure.

## Candidate ownership invariants

28. Candidate ownership checks compare enum members directly or compare exact lowercase values.
29. A transferred candidate is never passed to `abort()` by production cleanup.
30. An aborted candidate is not counted as a new abort attempt.
31. Candidate ownership transfer is recorded only after `transfer_to_runtime_manager()` succeeds.
32. Runtime-swap commit does not imply candidate ownership transfer in diagnostics.
33. Candidate resources close exactly once before acceptance and zero times after acceptance.

## Lease-gate and publication invariants

34. `publication_epoch` increments only for an actual initial publication or committed generation publication.
35. Defensive gate repair never increments `publication_epoch` by itself.
36. A staged pending swap cannot be repaired by merely setting admission open.
37. A staged swap must be resolved through rollback or commit under runtime-manager synchronization.
38. Defensive repair returns a structured outcome and records whether it changed state.
39. No terminal cleanup path detaches waiters or leaves a staged swap with admission open.
40. Pending-swap diagnostics are cleared only after rollback or completed accepted finalization.

## Database invariants

41. Commit-call failure injection reaches the same handler as an exception from `aiosqlite.Connection.commit()`.
42. The database layer records `in_transaction` before rollback and after rollback when the driver exposes it.
43. Confirmed rollback requires:
    - rollback attempted;
    - rollback returned successfully;
    - `in_transaction` is definitively false.
44. Any other commit-call failure outcome is indeterminate.
45. An indeterminate connection is detached from normal database operations before the transaction lock is released.
46. The detached connection is closed with a bounded best-effort operation.
47. Subsequent database calls fail with a typed unavailable/invalidated error until a new connection is established.
48. Reload never publishes the candidate after a commit-call exception.
49. Diagnostics expose rollback attempt, rollback result, transaction state, connection invalidation, and reconnect requirement.
50. A confirmed clean rollback leaves the connection usable and persistence unchanged.

## Verification invariants

51. Tests named as partial-transition tests apply at least one real transition before another transition fails.
52. Tests named as post-acceptance observer tests inject after `mark_accepted()`.
53. Tests named as finalization retry tests invoke the production finalization executor rather than toggling booleans manually.
54. Retirement-failure tests verify the original old generation is eventually scheduled and closed.
55. Commit-error tests patch `_commit_connection()` or the real connection commit path rather than using only the pre-call bypass seam.
56. Exact candidate close counts are asserted for every failure class.
57. Every accepted failure path asserts that transaction state is never `ABORTED`.
58. The exact implementation head passes the focused suite on Python 3.11 and 3.12.

---

# Workstream A — Make the production caller own partial transition state

## A1. Construct `TransitionApplyResult` before application

Change the transactional reload path from helper-return ownership:

```python
transition_result = await self._apply_process_transitions(plan)
```

to caller ownership:

```python
transition_result = TransitionApplyResult(plan)
await transition_result.apply_all()
```

The variable must be assigned before `apply_all()` begins.

`_apply_process_transitions()` should either:

- be removed from the production transactional path; or
- accept an existing `TransitionApplyResult` and apply that object without replacing it.

Do not depend on attaching the result to an exception and expecting a generic caller to recover it later.

## A2. Preserve typed apply failure context

`ProcessTransitionApplyError` should continue to expose:

- failed transition name;
- failed transition index;
- applied transition names;
- original exception.

It may retain `transition_result` as a diagnostic convenience, but that attribute must not be the only ownership route used by production cleanup.

At the production catch boundary, explicitly handle `ProcessTransitionApplyError` before a generic `Exception` catch so diagnostics classify transition apply failure without string inspection.

## A3. Make rollback state retryable

Current `TransitionApplyResult.rollback_applied()` marks `_rolled_back=True` even when one or more concrete rollbacks fail. Replace this with explicit state, for example:

```python
class TransitionRollbackState(enum.Enum):
    NOT_ATTEMPTED = "not_attempted"
    COMPLETE = "complete"
    PARTIAL = "partial"
```

Track remaining unrestored transitions.

Required behavior:

- first rollback attempts all applied transitions in reverse order;
- successful transitions are removed from the remaining set;
- failed transitions remain retryable;
- a second call retries only failed transitions;
- `COMPLETE` is set only when no unrestored transition remains;
- diagnostics preserve all attempt and failure history.

## A4. Do not swallow finalization failures

`TransitionApplyResult.finalize_all()` currently logs individual failures and sets `_finalized=True` unconditionally.

Replace this with a structured `TransitionFinalizeOutcome` containing:

- attempted transition names;
- finalized transition names;
- failures with error class and message;
- remaining transition names.

Finalization state is complete only when every applied transition is finalized. A retry invokes only remaining transitions.

## A5. Production-path transition test hook

Add a narrow test seam that can supply a custom `ProcessTransitionPlan` or custom transition tuple to the production reload path. Prefer dependency injection or monkeypatching `_prepare_process_transitions()` in tests rather than a broad production configuration option.

The required production test plan is:

- transition A applies successfully;
- transition B raises during apply;
- transition C is never called;
- SQLite rolls back;
- pending swap rolls back;
- transition A rolls back exactly once;
- candidate resources close exactly once;
- old generation remains active and accepting;
- no candidate lease becomes visible;
- subsequent reload succeeds.

### Acceptance criteria — Workstream A

- [ ] `TransitionApplyResult` is assigned in the reload owner before `apply_all()`.
- [ ] The production cleanup helper receives the same object on apply failure.
- [ ] A/B/C production integration test proves A rollback, B failure, C non-application.
- [ ] Partial rollback failures remain retryable.
- [ ] `rollback_applied()` does not report complete rollback while failures remain.
- [ ] `finalize_all()` returns structured failures and does not mark complete prematurely.
- [ ] No test described as partial-transition coverage uses a fault injected before transition application.

---

# Workstream B — Make acceptance a structural control-flow boundary

## B1. End the preacceptance `try` immediately after acceptance

Refactor the commit flow into two functions or two non-overlapping blocks:

```python
accepted_context = await self._commit_preacceptance(...)
return await self._finish_accepted_reload(accepted_context)
```

`_commit_preacceptance()` owns only rollback-capable work:

1. persistence transaction;
2. staged swap;
3. process transition application;
4. SQLite commit confirmation;
5. runtime swap commit;
6. `txn.mark_runtime_swap_committed()`;
7. `txn.mark_accepted()`;
8. creation and registration of an accepted-finalization job.

It must return immediately after the accepted-finalization job is registered.

No observer callback or other optional await may execute between `txn.mark_accepted()` and leaving the rollback-capable block.

## B2. Branch exclusively on `txn.reload_accepted`

Every outer exception and cancellation handler must start with:

```python
if txn.reload_accepted:
    ... post-acceptance handling only ...
else:
    ... preacceptance abort ...
```

Do not infer acceptance from:

- `publication_occurred`;
- `TransactionState.RUNTIME_PUBLISHED`;
- `TransactionState.RUNTIME_SWAP_COMMITTED`;
- a broad `is_committing` set;
- pending-swap state alone.

Once accepted, `mark_aborting()` and `mark_aborted()` must reject use or be guarded so they cannot be called.

Consider adding a transaction-level assertion:

```python
if self._reload_accepted:
    raise TransactionStateError("accepted reload cannot abort")
```

inside `mark_aborting()`.

## B3. Move `on_publish_complete()` after acceptance

`on_publish_complete()` is observational and must not be allowed to trigger preacceptance rollback.

Choose one of these patterns:

- invoke it as an idempotent accepted-finalization step; or
- invoke it through a non-raising `_safe_observer_call()` that records observer failure and continues.

Observer failure after acceptance must produce:

- candidate remains active;
- candidate resources remain open;
- transitions are not rolled back;
- transaction remains accepted;
- retirement remains scheduled or pending under a retained finalization job;
- operator-visible observer error diagnostics.

## B4. Correct ownership facts

Remove `_ownership_transfer_pending=False` from `mark_runtime_swap_committed()`.

Set ownership transfer complete only after `candidate.transfer_to_runtime_manager()` returns successfully.

Likewise, do not infer mirror completion, transition finalization, retirement scheduling, or transaction completion from an earlier state transition.

## B5. Remove legacy post-publication compensation from accepted flow

The legacy `RUNTIME_PUBLISHED` compensation path may remain only for a clearly separate legacy publication API if still used.

The staged-swap reload path must not enter `_compensate_post_publication()` after `txn.reload_accepted`.

Accepted finalization is forward-only completion, not compensation and not rollback.

### Acceptance criteria — Workstream B

- [ ] The preacceptance exception block ends immediately after accepted-finalization registration.
- [ ] `on_publish_complete()` cannot enter `_abort_precommit_reload()`.
- [ ] `txn.reload_accepted` is the sole pre/post boundary discriminator.
- [ ] `mark_aborting()` rejects an accepted transaction.
- [ ] Candidate ownership is not marked transferred at runtime-swap commit.
- [ ] An injected observer failure after acceptance leaves the active candidate healthy and the transaction accepted.
- [ ] No accepted staged-swap path uses legacy post-publication compensation.

---

# Workstream C — Add an executable accepted-finalization job

## C1. Introduce `AcceptedReloadFinalizationJob`

Add a process-owned object, preferably in `reload_transaction.py` or a focused `accepted_finalization.py` module.

Suggested fields:

```python
@dataclass
class AcceptedReloadFinalizationJob:
    request_id: str
    generation_id: int
    old_generation_id: int | None
    transaction: ReloadTransaction
    candidate: RuntimeGenerationCandidate
    pending_swap: PendingGenerationSwap
    transition_result: TransitionApplyResult
    published_generation: RuntimeGeneration
    app: Any | None
    observer: ReloadObserver
    state: AcceptedFinalizationState
    attempts: int = 0
    last_error_step: str | None = None
    last_error_class: str | None = None
    last_error_message: str | None = None
```

Use an explicit state enum rather than unrelated booleans, for example:

```text
REGISTERED
OWNERSHIP_TRANSFERRED
MIRROR_UPDATED
TRANSITIONS_FINALIZED
OBSERVER_REPORTED
RETIREMENT_SCHEDULED
COMPLETED
DEGRADED
```

A per-step completion record may supplement the enum, but there must be one executable owner.

## C2. Register before the first post-acceptance await

After runtime commit and `txn.mark_accepted()`:

1. construct the job synchronously;
2. register it in a process-owned registry keyed by generation/request ID;
3. only then perform any post-acceptance await.

The registry may live on `ReloadManager` if reload admission and shutdown always consult it. It must not be stored only in a local variable.

## C3. Implement idempotent `run()`

`run()` executes only incomplete steps:

1. transfer candidate ownership;
2. update app compatibility mirror;
3. finalize process transitions;
4. report publication completion through a safe observer wrapper;
5. schedule old-generation retirement through `pending_swap.finalize_retirement()`;
6. mark transaction states/facts complete;
7. remove the job from the registry.

Every step must satisfy:

- completed steps are not repeated;
- a failed step is recorded and remains first incomplete;
- exceptions do not invoke preacceptance cleanup;
- cancellation leaves the job registered;
- retirement scheduling is exactly once through pending-swap idempotence.

## C4. Define post-acceptance cancellation behavior

When the reload task is cancelled after acceptance:

1. do not call `mark_aborting()` or `mark_aborted()`;
2. create or retain the accepted-finalization job;
3. run a bounded shielded critical prefix sufficient to preserve ownership:
   - ensure job registration;
   - transfer candidate ownership if not yet transferred;
4. attempt bounded finalization under `asyncio.shield()`;
5. if the bound expires or cancellation is reasserted, retain the job as pending/degraded;
6. emit accepted-with-pending-finalization diagnostics;
7. propagate cancellation only after state is retained safely.

The final transaction state may remain `RUNTIME_SWAP_COMMITTED` with `reload_accepted=True`, or use a new explicit accepted-pending-finalization state. It must not become `ABORTED`.

## C5. Retry before new reload admission

Before setting `_reload_claimed=True` for a new reload:

- inspect the accepted-finalization registry;
- attempt bounded completion of pending jobs;
- admit the new reload only if all jobs are complete;
- otherwise raise a typed error such as `ReloadFinalizationPendingError` with safe diagnostics.

Do not clear a committed pending swap to make room for a new one.

## C6. Retry during shutdown

Shutdown must:

1. prevent new reload admission;
2. attempt bounded accepted-finalization completion;
3. report unresolved jobs;
4. retain old slots for runtime-manager shutdown cleanup where possible;
5. avoid silently discarding job references before resource shutdown.

### Acceptance criteria — Workstream C

- [ ] Every accepted reload has a registered executable finalization job.
- [ ] The job is registered before the first post-acceptance await.
- [ ] `run()` resumes from the first incomplete step.
- [ ] Post-acceptance cancellation never produces `ABORTED`.
- [ ] A second reload cannot silently clear an unresolved committed swap.
- [ ] Pending jobs are retried before new reload admission.
- [ ] Pending jobs are attempted during shutdown.
- [ ] Diagnostics expose job state, attempt count, first incomplete step, and last error.

---

# Workstream D — Retain and complete old-generation retirement ownership

## D1. Do not force-clear committed swaps

Change `RuntimeManager.prepare_candidate_swap()` so an existing committed but not finalized swap is not cleared defensively.

Required behavior:

- `PREPARED` or `STAGED`: reject as swap in progress;
- `COMMITTED`: reject as accepted finalization pending;
- `ROLLED_BACK` or `FINALIZED`: may be absent/cleared;
- unknown/inconsistent state: raise a typed invariant error and report diagnostics.

A committed swap is the authoritative owner of the old slot until `finalize_retirement()` succeeds.

## D2. Make retirement scheduling retryable

`PendingGenerationSwap.finalize_retirement()` must remain idempotent.

If `_spawn_retirement_task()` fails before task registration:

- swap remains `COMMITTED`;
- old slot remains retained;
- finalization job records retirement pending;
- retry invokes `finalize_retirement()` again.

If task creation succeeds but later retirement fails:

- the runtime manager retains failure diagnostics by generation ID;
- the slot reaches a terminal `FAILED_CLOSE` or equivalent state;
- the finalization job may complete scheduling while resource-close failure remains separately visible.

## D3. Verify original old generation, not merely subsequent reload success

Retirement failure tests must retain the original `old_generation_id` and assert after retry:

- a retirement task was registered for that exact generation;
- the slot no longer accepts leases;
- drain behavior is respected;
- resources close exactly once after leases drain or deadline expires;
- the slot reaches `CLOSED` or explicit `FAILED_CLOSE`;
- no old slot disappears merely because another reload succeeded.

## D4. Bound retained jobs

Because only one reload is admitted at a time and a new reload is blocked while finalization is pending, the normal bound is one accepted-finalization job.

Still enforce a configured or hard maximum and treat overflow as an invariant violation. Do not create an unbounded list of orphaned old generations.

### Acceptance criteria — Workstream D

- [ ] `prepare_candidate_swap()` never clears `COMMITTED` state.
- [ ] Retirement scheduling failure leaves an executable retry owner.
- [ ] Retry schedules the exact original old generation.
- [ ] Tests verify original old-slot close, not just a later successful reload.
- [ ] Runtime diagnostics distinguish retirement scheduled, running, closed, and failed-close.
- [ ] No accepted cancellation or finalization failure can orphan an old slot.

---

# Workstream E — Enforce database connection invalidation

## E1. Record actual transaction state

In the commit-call exception handler, capture:

```python
in_transaction_before_rollback: bool | None
in_transaction_after_rollback: bool | None
```

Do not leave `transaction_still_active` permanently `None` when the driver exposes the property.

## E2. Define confirmed rollback precisely

`rollback_succeeded=True` only when:

- rollback was required or explicitly attempted;
- rollback call returned without error;
- `in_transaction_after_rollback is False`.

If `commit()` raises while `in_transaction` is already false, the outcome is not automatically a clean rollback. The commit may have succeeded before the transport/future reported an error. Classify this as indeterminate unless the driver provides stronger evidence.

## E3. Add `_invalidate_connection_after_commit_failure()`

When the outcome is indeterminate:

1. atomically detach the connection from `Database._conn` while the connection lock is held;
2. set an explicit invalidated/degraded flag and reason;
3. close the detached connection with a bounded best-effort await;
4. prevent the connection object from being reused even if close raises;
5. require `connect()` or an explicit reconnect operation before future database access.

All subsequent operations must fail with a typed `DatabaseUnavailableError` or `DatabaseConnectionInvalidatedError`, not a generic attribute error.

## E4. Expose operational diagnostics

Add safe database diagnostics:

- connection state: connected / invalidated / disconnected;
- invalidation reason class;
- invalidated at timestamp;
- reconnect required;
- last commit outcome;
- rollback attempted/succeeded;
- in-transaction before/after;
- close attempted/succeeded.

Do not expose SQL values, credentials, or file contents.

## E5. Add actual commit-call tests

Required cases:

1. `_commit_connection()` raises while transaction remains active; rollback succeeds and connection remains usable.
2. `_commit_connection()` raises and rollback raises; connection is detached and future use fails typed.
3. `_commit_connection()` raises after fake connection reports `in_transaction=False`; classify indeterminate and invalidate.
4. close of invalidated connection also raises; connection remains detached and diagnostics record close failure.
5. reload using each failure never publishes the candidate and restores old admission.
6. pre-call bypass seam remains as a distinct test and is named accordingly.

### Acceptance criteria — Workstream E

- [ ] `transaction_still_active` or equivalent fields contain real observations.
- [ ] Indeterminate outcomes detach the connection before releasing the transaction lock.
- [ ] Future operations fail typed until reconnect.
- [ ] Confirmed rollback keeps the connection usable.
- [ ] Commit-call tests patch the actual commit wrapper/path.
- [ ] Reload never publishes on any commit-call exception.
- [ ] Database diagnostics expose reconnect-required state.

---

# Workstream F — Correct cleanup, gate repair, and diagnostics

## F1. Fix candidate ownership comparison

Replace string comparisons such as:

```python
state_val not in ("TRANSFERRED", "ABORTED")
```

with direct enum comparisons:

```python
candidate_state not in {
    CandidateOwnershipState.TRANSFERRED,
    CandidateOwnershipState.ABORTED,
}
```

For test doubles, normalize to exact lowercase enum values only in a dedicated helper.

Return truthful cleanup facts:

- abort not attempted for transferred candidates;
- abort not re-attempted for aborted candidates;
- no false successful abort count.

## F2. Restrict defensive gate repair

Replace `ensure_reload_gate_released()` with a state-aware API returning a structured outcome.

Rules:

- if no gate is active: no-op, no epoch change;
- if a staged swap exists: do not clear gate directly; require rollback through that swap;
- if no pending swap exists but gate is active: clear gate under condition lock, notify waiters, record invariant repair, do not increment publication epoch;
- if a committed swap exists: admission should already be open; repair admission only if needed, retain the committed swap;
- never make a staged candidate visible.

The reload `finally` block should not call a broad repair unconditionally. It should call state-specific cleanup before finalization and use defensive repair only after an invariant check fails.

## F3. Make publication epoch factual

Increment `_publication_epoch` only in:

- `install_initial()`;
- successful legacy candidate publication if still supported;
- successful `PendingGenerationSwap.commit()`.

Rollback, timeout, cancellation, no-op reload, validation rejection, and defensive repair must not increment it.

Add tests asserting exact epoch deltas for every terminal outcome.

## F4. Make transaction diagnostics truthful

Correct facts so they flip at the actual operation:

- ownership transfer after transfer call;
- mirror updated after mirror call;
- transitions finalized only after all transition finalizers succeed;
- observer reported after safe observer completion;
- retirement scheduled after task registration;
- transaction completed after all required finalization steps complete.

For pending accepted finalization, diagnostics must report:

- `reload_accepted=True`;
- current transaction/finalization state;
- `publication_occurred=True`;
- active candidate generation ID;
- first incomplete finalization step;
- old generation ID;
- retry attempts;
- last finalization error;
- candidate ownership state;
- retirement pending status.

Do not classify this as a clean abort.

### Acceptance criteria — Workstream F

- [ ] Candidate state checks use enum-safe comparisons.
- [ ] Transferred/aborted candidates are not passed to abort by production cleanup.
- [ ] Defensive gate repair is state-aware and does not bump publication epoch.
- [ ] Epoch changes exactly once per real publication and zero times otherwise.
- [ ] Accepted-pending-finalization diagnostics are distinct from aborted reloads.
- [ ] Ownership, mirror, transition, observer, and retirement facts flip only after their operations succeed.

---

# Workstream G — Replace overstated tests with exact production-boundary proof

## G1. Remove or rename false-claim tests

Correct tests including, but not limited to:

- a “post-acceptance exception” test that performs only a successful reload;
- a “partial transition failure” test that injects before transitions run;
- a cleanup test claiming `on_publish_started` occurs after staging when it occurs before swap preparation/staging;
- an “idempotent finalization retry” test that manually toggles booleans rather than running the executor;
- close-count tests that assert only admission state rather than instrumenting actual candidate resources.

A test name and docstring must match the boundary it actually exercises.

## G2. Required production-path tests

Add deterministic tests for:

### Transition apply failure

- A applies;
- B fails;
- C untouched;
- A rollback through `ReloadManager.reload()`;
- exact candidate close counts;
- unchanged persistence and active generation.

### Transition rollback partial failure

- A and B apply;
- later transition fails;
- B rollback fails first attempt;
- A rollback succeeds;
- cleanup outcome degraded;
- retry invokes only B;
- final outcome becomes restored.

### Observer failure after acceptance

- inject from `on_publish_complete()` after accepted-finalization job registration;
- candidate active and resources open;
- no transition rollback;
- transaction accepted, not aborted;
- job retries observer/reporting or safely records permanent observer failure;
- original old generation eventually retires.

### Post-acceptance cancellation

Inject cancellation at each boundary:

1. immediately after finalization-job registration;
2. during candidate ownership transfer wrapper if made awaitable/testable;
3. after transfer before mirror;
4. during mirror wrapper;
5. during transition finalization;
6. before observer completion;
7. before retirement scheduling;
8. during retirement scheduling wrapper.

For every boundary:

- candidate remains active;
- transaction remains accepted;
- no abort/rollback invoked;
- job remains registered or completes;
- original old generation eventually retires;
- next reload is admitted only after finalization resolves.

### Retirement scheduling failure

- exact original old generation retained;
- next reload initially rejected or triggers bounded retry;
- retry schedules retirement;
- old generation resources close exactly once;
- committed swap reaches `FINALIZED`.

### Database commit-call failure

- confirmed rollback case;
- rollback failure case;
- commit ambiguity case;
- connection close failure case;
- future-use typed failure after invalidation;
- reconnect restores operation where supported.

### Gate repair and epoch

- no-op final cleanup does not increment epoch;
- rollback does not increment epoch;
- commit increments exactly once;
- defensive repair without publication does not increment epoch;
- staged swap cannot be ungated without rollback.

## G3. Deterministic synchronization

Prefer barriers/events/hooks over sleeps.

The existing 1,000-iteration lease schedule may remain, but remove fixed 10 ms sleeps per iteration where possible. Use a barrier confirming the waiter is inside the condition wait before commit.

No correctness proof should depend only on scheduling luck or a wide timeout.

## G4. Exact close instrumentation

Instrument actual candidate-owned closeables supplied through the reload builder or a test factory.

For each failure class assert:

- preacceptance failure: each candidate resource closes once;
- repeated cleanup: still once;
- accepted failure/cancellation: candidate resources close zero times;
- eventual retirement: old-generation resources close once;
- process-owned resources close zero times during reload cleanup.

### Acceptance criteria — Workstream G

- [ ] No remaining test title overstates the boundary it exercises.
- [ ] Production partial-transition failure is covered.
- [ ] Production observer failure after acceptance is covered.
- [ ] All listed post-acceptance cancellation boundaries are covered.
- [ ] Retirement retry proves closure of the original old generation.
- [ ] Actual commit-call exception paths are covered.
- [ ] Epoch and gate-repair facts are covered.
- [ ] Close-count assertions use actual reload-owned resources.

---

# Workstream H — Focused CI and closure evidence

## H1. Add a dedicated Plan 018 CI job

Add `plan-018-reload-closure` to `.github/workflows/ci.yml` with Python 3.11 and 3.12 matrix coverage.

The job must include at least:

```bash
uv run pytest \
  tests/unit/test_runtime_manager.py \
  tests/unit/test_process_transition_plan.py \
  tests/unit/test_reload_manager.py \
  tests/unit/test_reload_diagnostics_matrix.py \
  tests/unit/test_database_commit_recovery.py \
  tests/integration/reload/test_plan_017_lease_condition.py \
  tests/integration/reload/test_plan_018_transition_ownership.py \
  tests/integration/reload/test_plan_018_accepted_finalization.py \
  tests/integration/reload/test_plan_018_retirement_retry.py \
  tests/integration/reload/test_plan_018_database_commit_failure.py \
  tests/integration/reload/test_pending_swap_visibility.py \
  -q --tb=short
```

Use actual final file names, but keep the coverage categories explicit.

Also run:

```bash
uv run python scripts/audit_xfail_skips.py
```

The job must not require network access or live provider credentials.

## H2. Run repository-required checks

Before claiming completion, run exactly:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest
uv run python scripts/audit_xfail_skips.py
```

Run the focused Plan 018 suite repeatedly enough to expose lifecycle leaks:

```bash
for i in 1 2 3; do
  uv run pytest <plan-018-focused-files> -q --tb=short
 done
```

## H3. Archive exact-head evidence

Record in the implementation commit or a status artifact:

- exact implementation commit SHA;
- Python versions;
- focused commands;
- pass/fail counts;
- skip/xfail counts and rationales;
- 1,000-iteration lease schedule result;
- transition partial-failure result;
- post-acceptance cancellation matrix result;
- retirement retry result;
- database invalidation result;
- complete required-check result;
- CI workflow run URLs or IDs.

No completion claim is valid when the evidence belongs to a different commit.

### Acceptance criteria — Workstream H

- [ ] A dedicated Plan 018 CI job runs on Python 3.11 and 3.12.
- [ ] The job includes all new exact-boundary tests.
- [ ] Skip/xfail audit runs in the focused job.
- [ ] Full required checks pass at the exact implementation head.
- [ ] Exact-head CI evidence is archived.
- [ ] No undocumented reload-related skip or non-strict xfail remains.

---

# Ordered milestones

## Milestone 1 — Transition ownership and truthful cleanup

Implement Workstream A and candidate enum corrections from F1.

Exit gate:

- production A/B/C transition failure restores A;
- partial rollback remains retryable;
- candidate close counts are exact;
- no uppercase/lowercase ownership comparison remains.

## Milestone 2 — Structural acceptance boundary

Implement Workstream B.

Exit gate:

- no post-acceptance await remains in rollback-capable control flow;
- observer failure cannot abort active resources;
- accepted transactions cannot call `mark_aborting()`.

## Milestone 3 — Accepted-finalization and retirement ownership

Implement Workstreams C and D.

Exit gate:

- every accepted reload has an executable retained job;
- cancellation leaves accepted state;
- committed swaps are never force-cleared;
- original old generation retires after retry.

## Milestone 4 — Database invalidation and runtime diagnostics

Implement Workstream E and remaining Workstream F items.

Exit gate:

- actual commit-call failure classifications are correct;
- indeterminate connection cannot be reused;
- epoch and gate-repair facts are truthful;
- accepted-pending-finalization diagnostics are operator-visible.

## Milestone 5 — Verification and exact-head evidence

Implement Workstreams G and H.

Exit gate:

- false-claim tests are removed or corrected;
- production boundaries are deterministically tested;
- Python 3.11/3.12 focused CI is green;
- full required checks are green at exact head.

---

# Expected file targets

Likely production targets:

- `src/eggpool/control/reload_manager.py`
- `src/eggpool/reload_transaction.py`
- `src/eggpool/runtime_manager.py`
- `src/eggpool/db/connection.py`
- `src/eggpool/errors.py`
- optional focused module such as `src/eggpool/control/accepted_finalization.py`
- `.github/workflows/ci.yml`
- `AGENTS.md` and architecture documentation only where operational semantics change.

Likely test targets:

- `tests/unit/test_process_transition_plan.py`
- `tests/unit/test_runtime_manager.py`
- `tests/unit/test_reload_manager.py`
- `tests/unit/test_reload_diagnostics_matrix.py`
- new `tests/unit/test_database_commit_recovery.py`
- replacement or correction of `tests/integration/reload/test_plan_017_transition_cleanup.py`
- replacement or correction of `tests/integration/reload/test_plan_017_acceptance_finalization.py`
- new Plan 018 focused integration files for transition ownership, accepted finalization, retirement retry, and commit failure.

Do not spread this pass into unrelated provider, dashboard, transcoder, or request-routing modules.

---

# Implementation review checklist

## Transition ownership

- [ ] Result constructed before apply.
- [ ] Production exception path retains result.
- [ ] Reverse rollback works.
- [ ] Partial rollback retry works.
- [ ] Finalization retry works.

## Acceptance

- [ ] Acceptance requires persistence and runtime commit.
- [ ] Accepted path cannot abort.
- [ ] Observer callback is post-acceptance safe.
- [ ] Ownership fact flips at actual transfer.

## Finalization

- [ ] Job registered before first post-acceptance await.
- [ ] Job retains candidate, swap, transition result, and old-generation ownership.
- [ ] Job runs idempotently.
- [ ] Cancellation retains job and accepted state.
- [ ] New reload resolves or rejects on pending job.
- [ ] Shutdown attempts pending jobs.

## Retirement

- [ ] Committed swap is not force-cleared.
- [ ] Retirement failure retains exact old slot.
- [ ] Retry schedules exact original old generation.
- [ ] Old resources close exactly once.

## Database

- [ ] Actual commit-call exception seam used.
- [ ] In-transaction before/after recorded.
- [ ] Confirmed rollback classification strict.
- [ ] Indeterminate connection detached and closed.
- [ ] Future access fails typed until reconnect.

## Gate and diagnostics

- [ ] Epoch increments only on publication.
- [ ] Defensive repair does not bump epoch.
- [ ] Staged swap cannot be ungated directly.
- [ ] Accepted-pending-finalization is not reported as abort.
- [ ] All completion facts are operation-derived.

## Verification

- [ ] Test names match actual boundaries.
- [ ] No timing-only correctness tests where barriers are possible.
- [ ] Plan 018 CI job exists for Python 3.11 and 3.12.
- [ ] Full required checks pass.
- [ ] Exact-head evidence is recorded.

---

# Global closure gate

Plan 018 is complete only when all statements below are true:

1. A production transition prefix remains reachable and is restored when a later transition fails.
2. Failed transition rollback remains retryable and is not falsely marked complete.
3. Transition finalization failure remains retryable and is not swallowed as completion.
4. No accepted reload can invoke preacceptance cleanup.
5. An observer exception after acceptance cannot close or roll back active-generation resources.
6. Post-acceptance cancellation never produces `ABORTED`.
7. Every accepted reload retains an executable finalization owner until complete.
8. A committed pending swap is never force-cleared while retirement is unresolved.
9. Retirement failure retains and later retires the exact original old generation.
10. Candidate ownership diagnostics flip only after actual transfer.
11. Transferred and aborted candidates are not incorrectly re-aborted.
12. Defensive gate repair cannot ungate an unresolved staged swap.
13. `publication_epoch` changes only on real publication.
14. Actual commit-call exceptions are classified through the production handler.
15. Indeterminate database connections are detached and cannot be reused.
16. Confirmed rollback leaves persistence, runtime generation, and connection usable state unchanged.
17. Tests exercise the boundaries their names claim.
18. Candidate and old-generation close counts are exact for all failure classes.
19. Focused Plan 018 tests pass on Python 3.11 and 3.12.
20. Full repository-required checks pass at the exact implementation commit.
21. Exact-head CI evidence is available.

Until every item passes, reload atomicity closure must remain unclaimed.
