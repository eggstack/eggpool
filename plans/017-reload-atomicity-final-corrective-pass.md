# Reload Atomicity Final Corrective Pass

Date: 2026-07-23
Status: completed
Depends on:

- `plans/015-reload-atomicity-final-closure.md`
- `plans/016-reload-atomicity-corrective-closure.md`

Implementation baseline: `c1eb802bfd387eb648722633226dbf0d360e148d`

## Objective

Close the remaining correctness gaps in the staged reload protocol after Plan 016 without reopening the broader reload architecture.

Plan 016 successfully introduced:

- a locked `PendingGenerationSwap` state machine;
- atomic candidate activation and old-generation admission closure;
- typed swap lifecycle errors;
- a publication epoch;
- fail-closed Linux peer credential validation;
- a true pre-`COMMIT` transaction-boundary fault seam;
- progress diagnostics;
- focused Python 3.11/3.12 CI coverage.

The remaining defects are narrower but still prevent closure:

1. `RuntimeManager.acquire()` can lose a publication notification between clearing and waiting on `_state_changed_event`, causing a request to stall until the generation lease timeout even when an eligible generation is already active.
2. `_apply_process_transitions()` still loses its `TransitionApplyResult` when `apply_all()` raises, so production reload cannot roll back a partially applied transition prefix.
3. precommit cleanup does not own candidate resource abort, and the outer generic exception path can skip candidate cleanup while the transaction remains in `RUNTIME_STAGED`.
4. failures or cancellation after `RUNTIME_SWAP_COMMITTED` can still run precommit rollback logic, omit retirement scheduling, or classify an accepted reload as aborted.
5. the current database seam raises before calling the real commit operation; there is no explicit recovery contract for an exception raised by `connection.commit()` itself.
6. tests do not exercise the exact staged-cancellation, distinct-generation, lost-wakeup, partial-transition, and real-commit-error boundaries required to prove closure.

This plan is complete only when those six defects are closed with deterministic tests and exact-head CI evidence.

---

# Scope

## In scope

- lease waiter synchronization and notification semantics;
- removal of direct lease-gate mutation from `ReloadManager`;
- retention of partially applied process-transition state;
- one precommit cleanup owner for swap rollback, transition rollback, candidate abort, and diagnostic aggregation;
- an explicit reload acceptance point;
- idempotent post-acceptance finalization;
- cancellation behavior before and after acceptance;
- actual SQLite/aiosqlite commit-call failure handling;
- rollback-confirmed versus indeterminate persistence outcomes;
- distinct-generation barrier tests;
- deterministic stress and CI verification for these boundaries.

## Explicit non-goals

Do not:

- replace the generation/lease architecture;
- redesign config parsing, validation, or diff classification;
- add a distributed transaction coordinator;
- make `app.state` request-authoritative;
- change provider routing, transcoding, compression, metrics, or dashboard behavior;
- replace SQLite or aiosqlite;
- redesign process supervision outside the reload transition lifecycle;
- expand the control protocol;
- refactor unrelated legacy reload code solely for style;
- claim general crash consistency beyond the process and SQLite guarantees explicitly tested here.

This is a final corrective pass over the Plan 015/016 implementation, not another reload-system redesign.

---

# Required invariants

The implementation and tests must enforce these invariants.

## Lease admission invariants

1. Active-slot selection, admission eligibility validation, and lease-count increment are one operation under `RuntimeManager._lock`.
2. A waiter cannot miss a state change between checking the admission predicate and beginning to wait.
3. Commit, rollback, initial install, and shutdown notify all lease waiters while holding the same synchronization boundary used by the waiter predicate.
4. A waiter resumes immediately when an eligible active generation exists; it does not wait for an unrelated later publication.
5. No new old-generation lease is returned after candidate commit.
6. No candidate lease is returned before candidate commit.
7. Existing old-generation leases survive commit and drain normally.
8. A terminal reload path cannot detach or clear a lease-gate primitive without notifying its waiters.
9. `ReloadManager` does not directly mutate runtime-manager lease synchronization fields.
10. Lease waiter accounting returns to zero after success, timeout, cancellation, rollback, commit, and shutdown.

## Precommit cleanup invariants

11. The caller retains the exact `TransitionApplyResult` before the first transition is applied.
12. If transition N fails, transitions `0..N-1` remain reachable and are rolled back in reverse order.
13. The failed transition and transitions after it are not reported as successfully applied.
14. Every pre-acceptance failure or cancellation has one cleanup owner.
15. That cleanup owner attempts, in a deterministic order:
    - staged swap rollback;
    - applied transition rollback;
    - candidate resource abort;
    - runtime admission verification;
    - diagnostic aggregation.
16. Candidate resources are closed exactly once when acceptance did not occur.
17. Rollback failures do not mask the primary failure, but they are surfaced as a degraded cleanup outcome.
18. A cleanup helper never attempts to roll back a committed/accepted runtime swap.

## Acceptance and finalization invariants

19. Reload acceptance is represented by an explicit fact, not inferred from a broad transaction-state range.
20. Acceptance requires both:
    - confirmed SQLite commit success; and
    - committed runtime swap visibility.
21. Before acceptance, rollback and candidate abort are permitted.
22. After acceptance, process-transition rollback, runtime rollback, and candidate abort are forbidden.
23. Post-acceptance steps are idempotent and retryable:
    - candidate ownership transfer;
    - compatibility mirror update;
    - transition finalization;
    - retirement scheduling;
    - transaction completion bookkeeping.
24. Failure after acceptance leaves the candidate generation authoritative and records an accepted reload with pending/degraded finalization, never a clean abort.
25. Cancellation after acceptance completes or records bounded post-acceptance finalization before propagating cancellation.
26. Retirement scheduling is attempted exactly once or retried idempotently from an explicit pending-finalization record.
27. An accepted reload cannot leave the old generation accepting new leases.

## Persistence outcome invariants

28. A commit-call exception with confirmed rollback leaves persistence and runtime on the old generation.
29. A commit-call exception must trigger a rollback attempt when SQLite still reports an active transaction.
30. A failed rollback or ambiguous transaction state is classified as persistence outcome indeterminate, not as a confirmed clean rollback.
31. An indeterminate persistence outcome does not publish the candidate generation.
32. An indeterminate persistence outcome marks the database/reload subsystem degraded and requires operator-visible remediation.
33. A database connection that cannot be proven usable after commit/rollback failure is closed or invalidated before reuse.
34. Test injection reaches the same exception handler as a real `connection.commit()` exception.
35. The existing pre-commit bypass seam remains available only if it tests a distinct boundary and is clearly named as such.

## Verification invariants

36. Candidate-visibility tests use distinct old and candidate generation IDs and digests.
37. Lost-wakeup tests use deterministic barriers rather than timing-only sleeps.
38. Cancellation is injected after swap staging and after runtime commit, not only before candidate construction.
39. Partial-transition failure is tested through the production reload path.
40. Candidate resource closure is asserted with exact close counts.
41. The exact implementation head passes the focused suite on Python 3.11 and 3.12.

---

# Workstream A — Replace event clearing with predicate-based condition waiting

## A1. Use one `asyncio.Condition` backed by the runtime-manager lock

Replace the `_state_changed_event.clear()` / `.set()` protocol with a condition variable that uses `RuntimeManager._lock` as its lock.

Preferred initialization:

```python
self._lock = asyncio.Lock()
self._lease_condition = asyncio.Condition(self._lock)
```

The condition predicate must be evaluated while the condition lock is held.

The authoritative admission predicate is:

```python
def _lease_claim_available_locked(self) -> bool:
    return (
        self._shutdown_in_progress
        or (
            not self._lease_admission_gated
            and self._active is not None
            and self._active.accepting_leases
        )
    )
```

A condition wait atomically releases the lock and registers the waiter, eliminating the check/clear/wait gap.

Do not use an `asyncio.Event` whose state is cleared outside the same lock used to validate the predicate.

## A2. Represent gating as state, not ownership of an event object

Replace `_lease_gate_event: asyncio.Event | None` as the authoritative gate with an explicit boolean or enum owned by `RuntimeManager`, for example:

```python
self._lease_admission_gated: bool = False
```

The pending swap may retain a read-only association for diagnostics, but it must not own a separate notification primitive that can be detached from blocked waiters.

Required locked changes:

- stage: set `_lease_admission_gated=True`;
- commit: activate candidate, close old admission, set `_lease_admission_gated=False`, increment epoch, `notify_all()`;
- rollback: restore old active slot, set `_lease_admission_gated=False`, `notify_all()`;
- initial install: increment epoch and `notify_all()`;
- shutdown: set shutdown state, clear/close admission as required, `notify_all()`.

All notifications must happen while holding `self._lease_condition` / `self._lock`.

## A3. Simplify `acquire()` to a locked claim plus condition wait

The acquire loop should have this shape:

```python
async with self._lease_condition:
    self._lease_gate_waiters += 1
    try:
        await asyncio.wait_for(
            self._lease_condition.wait_for(self._lease_claim_available_locked),
            timeout=remaining,
        )
        if self._shutdown_in_progress:
            raise RuntimeManagerLeaseExhaustedError(...)
        slot = self._active
        assert slot is not None and slot.accepting_leases
        slot.active_leases += 1
        self._acquire_id += 1
        return GenerationLease(...)
    finally:
        self._lease_gate_waiters -= 1
```

The exact waiter-count placement may differ to avoid counting immediate claims, but all increments and decrements must occur under the condition lock.

The current `epoch_before` / `epoch_after` release-and-retry logic is unnecessary once claim and publication are serialized by the same lock. Retain `_publication_epoch` for diagnostics and assertions, not as a substitute for correct waiter synchronization.

## A4. Remove terminal-path direct gate mutation

Delete production code equivalent to:

```python
self._runtime_manager._lease_gate_event = None
```

from `ReloadManager.finally` or any other external owner.

Terminal cleanup must use one of:

- `pending_swap.rollback()` before acceptance;
- `pending_swap.commit()` / `finalize_retirement()` after acceptance;
- a narrow defensive runtime-manager API such as `ensure_reload_gate_released(swap)` that:
  - acquires the runtime-manager lock;
  - validates swap ownership/state;
  - changes gate state only when safe;
  - notifies all waiters;
  - records a diagnostic if it repaired inconsistent state.

The defensive API must never silently make a staged candidate visible.

## A5. Preserve bounded timeout and cancellation semantics

`GENERATION_LEASE_TIMEOUT_S` remains the outer bound.

On timeout:

- waiter count decrements;
- no slot lease is incremented;
- a typed lease-exhaustion error is raised.

On cancellation:

- waiter count decrements;
- condition state is unchanged;
- no lease is leaked.

On shutdown:

- all waiters wake;
- all waiters fail promptly with the typed shutdown/lease-exhaustion error;
- no waiter remains parked until the normal timeout.

### Acceptance criteria — Workstream A

- [ ] `_state_changed_event.clear()` is absent from the production lease-acquisition path.
- [ ] Runtime admission waiting uses an `asyncio.Condition` or an equivalent predicate-based primitive with no check/register gap.
- [ ] Gate state, active slot, slot eligibility, lease increment, waiter count, and notifications are synchronized by the runtime-manager lock.
- [ ] Commit and rollback notify waiters under the same lock used by the admission predicate.
- [ ] `ReloadManager` no longer mutates runtime-manager gate/event fields directly.
- [ ] No fixed-interval polling is used for publication admission.
- [ ] A deterministic barrier test proves a notification cannot be lost between predicate evaluation and waiting.
- [ ] Commit and rollback wake blocked acquisitions in less than 250 ms under the test harness.
- [ ] Timeout, cancellation, and shutdown tests end with `lease_gate_waiter_count == 0`.
- [ ] The focused lease race test passes at least 1,000 deterministic iterations.

---

# Workstream B — Retain the partial transition owner through production failure

## B1. Construct `TransitionApplyResult` in the reload owner

Do not construct the result inside a helper that can raise before returning it.

Preferred production shape:

```python
transition_result = TransitionApplyResult(process_transition_plan)
await transition_result.apply_all()
```

The `transition_result` variable must be assigned before `apply_all()` begins.

The helper `_apply_process_transitions()` should either:

- be removed from the transactional path; or
- accept an already-created `TransitionApplyResult` and never replace it.

Do not rely on `ProcessTransitionApplyError.applied_transition_names` as the rollback owner. Names are diagnostics; the result object owns the applied transition objects and their snapshots.

## B2. Preserve the result on every exception class

If `apply_all()` raises:

- `transition_result` remains non-`None`;
- the partial `_applied` stack remains intact;
- the exception retains the failing transition identity and original cause;
- the shared precommit cleanup helper receives the result.

Do not catch and rebuild a second `TransitionApplyResult`, because that would not contain the old-state snapshots captured by the applied transitions.

## B3. Classify rollback aggregation explicitly

Replace log-only handling of rollback errors with a structured result such as:

```python
@dataclass(frozen=True)
class TransitionRollbackOutcome:
    attempted: tuple[str, ...]
    restored: tuple[str, ...]
    failures: tuple[TransitionRollbackFailure, ...]
```

`rollback_applied()` may return this richer type, or the cleanup helper may adapt its current list result.

The reload diagnostic must expose at least:

- whether transition rollback was attempted;
- applied transition names;
- restored transition names;
- failed rollback names and error classes;
- whether cleanup is degraded.

No raw configuration values or secrets belong in this diagnostic.

## B4. Correct transition lifecycle flags on failed rollback

A transition whose rollback raises must not be marked fully restored.

For `TaskSpecTransition`, avoid unconditional state mutation in a `finally` block that sets `_applied=False` and `_rolled_back=True` even when restoring old specs failed.

Required semantics:

- on rollback success:
  - `_applied=False`;
  - `_rolled_back=True`;
- on rollback failure:
  - retain a state such as `_rollback_failed=True`;
  - do not claim restoration;
  - preserve enough old-state metadata for diagnostics or a bounded retry;
  - re-raise to the aggregator.

Apply the same rule to every concrete transition.

## B5. Test the production wrapper, not only the aggregator

Create a production-path failure test with at least three transitions:

1. transition A applies successfully;
2. transition B raises during apply;
3. transition C must not apply;
4. shared cleanup rolls A back;
5. call order is `A.apply`, `B.apply`, `A.rollback`;
6. old process state is restored;
7. runtime and persistence remain old;
8. candidate resources are aborted exactly once.

Create a second case where A rollback raises:

- the primary B apply error remains the primary failure;
- diagnostic records A rollback failure;
- cleanup is classified degraded;
- no false `process_transitions_restored=True` fact is emitted.

### Acceptance criteria — Workstream B

- [ ] Production code assigns `TransitionApplyResult` before invoking `apply_all()`.
- [ ] A transition apply exception cannot discard the result object.
- [ ] The shared cleanup path receives the exact partially applied result.
- [ ] Applied transitions roll back in reverse order.
- [ ] A failed transition and all later transitions are never marked applied.
- [ ] Rollback failure does not mark the transition restored.
- [ ] Rollback aggregation is exposed in structured diagnostics.
- [ ] A production reload test proves partial-prefix rollback.
- [ ] A production reload test proves rollback-failure degradation without masking the primary exception.

---

# Workstream C — Make preacceptance cleanup the sole resource owner

## C1. Expand `_abort_precommit_reload()` inputs

The helper must own all preacceptance cleanup inputs:

```python
async def _abort_precommit_reload(
    *,
    txn: ReloadTransaction,
    pending_swap: PendingGenerationSwap | None,
    transition_result: TransitionApplyResult | None,
    candidate: RuntimeGenerationCandidate | None,
    cause: BaseException,
) -> PrecommitAbortOutcome:
    ...
```

Do not split candidate abort into a later outer exception block that depends on `txn.is_committing` or another broad state predicate.

The helper is called only when `txn.reload_accepted is False`.

## C2. Use deterministic cleanup ordering

Required order:

1. rollback staged swap;
2. rollback applied process transitions;
3. abort candidate resources;
4. verify old generation is active and admission is open;
5. capture cleanup diagnostics;
6. return a structured outcome.

Rationale:

- rolling back the swap first reopens request admission quickly;
- transition rollback restores process-owned state;
- candidate abort then closes generation-owned resources no longer reachable from runtime;
- verification detects any remaining mixed state.

Every step must be idempotent.

## C3. Shield the complete cleanup operation correctly

Calling `asyncio.shield(coro)` from an already-cancelled task can still raise `CancelledError` to the caller while the shielded child continues.

Use an explicit task and await its completion policy:

```python
cleanup_task = asyncio.create_task(_abort_precommit_reload(...))
try:
    outcome = await asyncio.shield(cleanup_task)
except asyncio.CancelledError:
    outcome = await cleanup_task
    raise
```

Or use an equivalent helper that guarantees bounded cleanup finishes before the reload releases its admission claim.

Do not log “shield cancelled” and continue while cleanup may still be running detached.

The cleanup operations are bounded:

- swap rollback should be immediate;
- transition rollback must use bounded transition operations;
- candidate abort already uses per-resource close limits.

## C4. Remove duplicate candidate-abort logic

After the shared helper owns candidate cleanup, remove or reduce the outer generic and cancellation handlers so they do not independently decide candidate ownership from:

- `txn.is_committing`;
- transaction state category;
- candidate internal state alone.

The authoritative rules are:

- before acceptance and candidate not transferred: abort exactly once;
- after acceptance: never abort;
- if cleanup already ran: return the same cleanup outcome.

## C5. Verify resource closure directly

Tests must instrument candidate resources with counters and assert:

- every registered candidate resource closes once on preacceptance failure;
- no candidate resource closes twice after duplicate cleanup calls;
- no active-generation resource is closed;
- no candidate resource is closed after acceptance;
- pending swap ownership is cleared;
- lease admission is open.

### Acceptance criteria — Workstream C

- [ ] `_abort_precommit_reload()` owns swap rollback, transition rollback, and candidate abort.
- [ ] Outer exception/cancellation handlers do not contain a second independent candidate-abort policy.
- [ ] Cleanup completion is awaited before the reload admission claim is released.
- [ ] Cancellation cannot leave a detached cleanup task mutating runtime after the caller has returned.
- [ ] Candidate resources close exactly once on transition failure, commit failure, and staged cancellation.
- [ ] Active-generation resources never close on those failures.
- [ ] Cleanup returns a structured outcome and diagnostics surface degraded substeps.

---

# Workstream D — Separate reload acceptance from post-acceptance finalization

## D1. Add an explicit acceptance fact

Add a fact such as:

```python
self.reload_accepted: bool = False
```

or a typed acceptance state.

Set it exactly once only after:

1. the outer SQLite transaction reports commit success; and
2. `pending_swap.commit()` returns successfully, making the candidate visible and closing old admission.

At that point:

- `publication_occurred=True`;
- `persistence_committed=True`;
- `active_generation_after` is the candidate generation;
- rollback is prohibited.

Do not infer acceptance from `is_committing`, `RUNTIME_PUBLISHED`, or a set of loosely related states.

## D2. Split preacceptance and post-acceptance try blocks

Required structure:

```python
# Preacceptance section
try:
    async with db.transaction():
        ...
    txn.mark_sqlite_committed()
    await pending_swap.commit()
    txn.mark_runtime_swap_committed(...)
    txn.mark_accepted()
except asyncio.CancelledError:
    await complete_preacceptance_abort(...)
    raise
except Exception:
    await complete_preacceptance_abort(...)
    raise

# Post-acceptance section
try:
    await _finalize_accepted_reload(...)
except asyncio.CancelledError:
    await _complete_or_record_accepted_finalization(...)
    raise
except Exception as exc:
    await _record_accepted_finalization_failure(...)
```

The preacceptance cleanup helper must be unreachable from the post-acceptance block.

## D3. Create one idempotent accepted-finalization object

Introduce a narrow object or record, for example:

```python
@dataclass
class AcceptedReloadFinalization:
    candidate_ownership_transferred: bool = False
    compatibility_mirror_updated: bool = False
    transitions_finalized: bool = False
    retirement_scheduled: bool = False
    transaction_completed: bool = False
```

It owns the exact post-acceptance steps and supports idempotent `complete()`.

Each step checks its fact before running.

Required ordering:

1. transfer candidate ownership;
2. update compatibility mirror;
3. finalize process transitions;
4. schedule old-generation retirement;
5. mark transaction completed.

A failure leaves the remaining facts pending; a retry resumes from the first incomplete step.

## D4. Correct ordinary exception handling after acceptance

Any exception with `txn.reload_accepted=True` must:

- not call `_abort_precommit_reload()`;
- not call `transition_result.rollback_applied()`;
- not call `candidate.abort()`;
- not restore the old active slot;
- retain the candidate as authoritative;
- record `accepted_with_finalization_pending` or `accepted_degraded`;
- attempt bounded idempotent finalization retry;
- expose exactly which steps remain incomplete.

A user-visible reload response should distinguish:

- fully completed success;
- accepted but finalization pending/degraded;
- preacceptance failure with old state preserved.

Do not return a generic clean “aborted” classification for an accepted candidate.

## D5. Correct cancellation after acceptance

On cancellation after acceptance:

1. create or retain the accepted-finalization record;
2. run `complete()` under bounded shielding;
3. if all steps complete, record accepted success/cancelled-after-acceptance and re-raise cancellation according to control protocol policy;
4. if a step fails, retain the finalization record, mark degraded, and re-raise cancellation;
5. do not roll back transitions or abort candidate resources.

Test cancellation at these barriers:

- immediately after `pending_swap.commit()`;
- after candidate ownership transfer;
- after mirror update;
- after transition finalization;
- immediately before retirement scheduling.

## D6. Handle persistence-committed/runtime-not-committed separately

There is a narrow boundary where SQLite commit may have succeeded but `pending_swap.commit()` fails.

Do not classify this as ordinary precommit rollback because persistence has already committed.

Use an explicit state such as:

- `PERSISTENCE_COMMITTED_RUNTIME_PENDING`.

Required handling:

- retry `pending_swap.commit()` when failure is known transient and state remains staged;
- if commit cannot complete, roll back the staged runtime gate to restore service admission;
- report persistence/runtime divergence explicitly;
- mark reload subsystem degraded;
- do not claim old persistence is unchanged;
- make the next reload or a dedicated reconciliation operation able to resynchronize persistence idempotently.

This boundary must not be conflated with an actual SQLite commit exception where commit success is unconfirmed.

## D7. Ensure retirement scheduling is not lost

If acceptance occurred and retirement scheduling fails:

- old admission remains closed;
- old generation remains tracked;
- finalization record retains old generation identity and drain timeout;
- retry is idempotent;
- duplicate retirement tasks are prevented;
- shutdown can discover and complete/force the pending retirement.

### Acceptance criteria — Workstream D

- [ ] `ReloadTransaction` has an explicit acceptance fact or typed equivalent.
- [ ] Acceptance is set only after confirmed SQLite commit and committed runtime swap.
- [ ] Preacceptance cleanup code is structurally separated from post-acceptance finalization code.
- [ ] `RUNTIME_SWAP_COMMITTED` is handled as post-publication/accepted once the acceptance fact is set.
- [ ] No accepted path calls transition rollback, swap rollback, or candidate abort.
- [ ] Candidate ownership transfer, mirror update, transition finalize, retirement scheduling, and completion are idempotent steps.
- [ ] Post-acceptance exception diagnostics identify pending finalization steps.
- [ ] Post-acceptance cancellation tests pass at every required barrier.
- [ ] A persistence-committed/runtime-not-committed failure has its own explicit degraded classification.
- [ ] Retirement scheduling failure cannot lose the old generation or allow new old-generation leases.

---

# Workstream E — Test and recover from the actual SQLite commit-call exception path

## E1. Separate the two fault seams by name

Retain the current seam only if renamed to make its boundary explicit, for example:

```python
TEST_INJECT_BEFORE_COMMIT_CALL
```

It represents failure after transaction body completion but before invoking SQLite commit.

Add a second seam that replaces or wraps the actual commit operation:

```python
async def _commit_connection(self) -> None:
    await self.connection.commit()
```

Tests patch `_commit_connection()` to raise. Production `transaction()` must call this method and handle its exception exactly as it would handle a real aiosqlite commit exception.

Do not raise the new seam before entering the actual commit exception handler.

## E2. Add a typed commit failure result

Introduce typed exceptions carrying recovery facts, for example:

```python
class DatabaseCommitError(DatabaseError):
    rollback_attempted: bool
    rollback_succeeded: bool
    transaction_still_active: bool | None
    connection_invalidated: bool
    outcome: Literal["rolled_back", "indeterminate"]
```

Do not rely on message parsing.

## E3. Recover when rollback is confirmable

When `_commit_connection()` raises:

1. inspect `connection.in_transaction` where available;
2. if the transaction is still active, attempt rollback;
3. confirm the transaction is no longer active;
4. if confirmed, raise `DatabaseCommitError(outcome="rolled_back")`;
5. leave the connection available only if a bounded probe confirms it is usable.

The reload layer then:

- rolls back the staged swap;
- rolls back process transitions;
- aborts candidate resources;
- keeps the old generation active;
- reports persistence not committed.

## E4. Fail closed on ambiguous commit outcome

If:

- `connection.in_transaction` is unavailable or inconsistent;
- rollback raises;
- the connection remains in a transaction;
- a post-failure probe cannot establish usability;

then:

- classify outcome `indeterminate`;
- invalidate or close the connection;
- prevent further writes through that connection;
- leave the old runtime generation active and reopen admission;
- do not publish the candidate;
- mark readiness/reload diagnostics degraded;
- provide an operator-visible error that restart/reconnect or database recovery is required.

An indeterminate commit may have reached disk; therefore diagnostics must not state that persistence is unchanged.

## E5. Avoid global cross-test injection state

Prefer an instance-scoped injection hook or context manager over a mutable class variable shared by every `Database` instance.

If a class-level seam remains temporarily:

- protect it against concurrent tests;
- reset it in `finally`;
- document that the focused suite must not run those cases concurrently;
- add a test proving another database instance is not unintentionally affected, or migrate to instance scope in this pass.

## E6. Add actual commit-call tests

Required tests:

### Case 1 — commit call raises, rollback succeeds

- transaction body completes;
- `_commit_connection()` is invoked and raises;
- rollback runs;
- outcome is `rolled_back`;
- persistence snapshot equals pre-reload state;
- old runtime generation remains active;
- lease admission is open;
- transitions restored;
- candidate resources closed exactly once.

### Case 2 — commit call raises, rollback also raises

- outcome is `indeterminate`;
- connection is invalidated/closed;
- candidate is not published;
- old runtime remains active for existing in-memory service;
- reload/readiness diagnostic is degraded;
- result does not claim persistence unchanged.

### Case 3 — commit call raises after SQLite reports no active transaction

Treat this as potentially committed/indeterminate unless the driver provides a stronger guarantee.

- candidate is not automatically published;
- diagnostic clearly records ambiguous persistence outcome;
- connection is invalidated or reconciled through an explicitly proven path.

### Case 4 — subsequent clean transaction

After a confirmed rollback and usable connection:

- a subsequent transaction succeeds;
- fault seam is one-shot/instance-scoped;
- no stale transaction context remains.

### Acceptance criteria — Workstream E

- [ ] Production transaction code has an exception handler around the actual commit call.
- [ ] A test seam raises from that handler path, not before it.
- [ ] Commit failure produces a typed outcome with rollback and connection facts.
- [ ] Confirmed rollback preserves old persistence and runtime.
- [ ] Ambiguous commit outcome is never reported as confirmed rollback.
- [ ] Ambiguous outcome invalidates or closes an untrusted connection.
- [ ] Candidate publication never occurs after an unconfirmed commit outcome.
- [ ] Candidate cleanup and lease reopening complete on confirmed and indeterminate failures.
- [ ] Subsequent transaction success is tested after confirmed rollback recovery.

---

# Workstream F — Deterministic closure tests and CI evidence

## F1. Build distinct synthetic generations for runtime-manager tests

Add a focused generation factory that creates old and candidate generations with:

- different generation IDs;
- different config digests;
- independently instrumented no-op closeable resources;
- independently tracked lease and retirement state.

Do not use the current active generation object as its own candidate in visibility tests.

## F2. Add deterministic lost-wakeup barriers

Provide test-only hooks or condition barriers at these points:

- acquire has evaluated admission as unavailable while holding the condition lock;
- waiter is about to call `condition.wait()`;
- commit/rollback attempts to acquire the condition lock;
- waiter resumes and claims a slot.

The test must prove that publication cannot notify between predicate check and waiter registration because the same condition lock serializes both actions.

Required assertions:

- waiter returns candidate after commit;
- waiter returns old generation after rollback;
- neither waits for the 30-second timeout;
- waiter count returns to zero;
- no old lease is issued after commit;
- no candidate lease is issued before commit.

Run both commit and rollback cases for at least 1,000 deterministic iterations or an equivalent state-machine schedule enumeration.

## F3. Add staged cancellation barriers

Inject cancellation at:

1. immediately after `pending_swap.stage()`;
2. after transition A applied and before transition B;
3. after the transaction body completes but before commit call;
4. after confirmed SQLite commit but before runtime swap commit;
5. immediately after runtime swap commit;
6. after ownership transfer;
7. before retirement scheduling.

Expected outcomes must be explicit per boundary.

Before acceptance:

- old runtime and persistence retained when rollback confirmed;
- gate reopened;
- transitions restored;
- candidate aborted once.

After acceptance:

- candidate remains active;
- no transition rollback;
- no candidate abort;
- finalization completes or is recorded pending;
- old admission remains closed.

## F4. Add production partial-transition tests

Use the actual reload transaction path, not direct `TransitionApplyResult` calls.

Assert:

- exact apply/rollback order;
- state restoration;
- diagnostic content;
- candidate close counts;
- no gate leak;
- no pending swap leak.

## F5. Add post-acceptance failure injection seams

Provide narrow test-only seams for:

- ownership transfer failure;
- mirror update failure;
- transition finalization failure;
- retirement scheduling failure.

Each test asserts:

- candidate remains active;
- old generation rejects new leases;
- no process-transition rollback occurs;
- candidate resources are not aborted;
- finalization record identifies remaining steps;
- retry completes without duplicate side effects.

## F6. Add exact resource-ownership assertions

For every preacceptance failure class:

- candidate close callbacks each run once;
- old-generation callbacks run zero times until legitimate retirement;
- pending candidate slot is unreachable after cleanup;
- pending swap is cleared;
- waiter count is zero.

For every post-acceptance failure class:

- candidate close callbacks run zero times;
- old generation remains tracked for retirement;
- ownership transfer/finalization retry is idempotent.

## F7. Update focused CI

Update the Plan 016-focused job or replace it with a final closure job named clearly, for example:

```yaml
reload-atomicity-final-closure:
  strategy:
    matrix:
      python-version: ["3.11", "3.12"]
```

The command must include:

- runtime manager condition/wakeup tests;
- pending swap state tests;
- distinct-generation visibility tests;
- staged and post-acceptance cancellation tests;
- production partial-transition rollback tests;
- actual commit-call failure tests;
- control socket tests;
- diagnostics tests;
- reload fault matrix;
- skip/xfail audit.

Add a separate deterministic stress command if the 1,000-iteration barrier test is not part of normal pytest collection.

## F8. Record exact-head evidence

Closure evidence must record:

- implementation commit SHA;
- Python version;
- focused command;
- test counts and result;
- full unit/integration result;
- `ruff format --check` result;
- `ruff check` result;
- `pyright` result;
- `audit_xfail_skips.py` result;
- CI run URL or workflow run identity for the exact implementation SHA.

Do not cite results from a predecessor commit as closure evidence.

### Acceptance criteria — Workstream F

- [ ] Visibility tests use distinct old and candidate identities.
- [ ] A deterministic lost-wakeup barrier test exists for commit and rollback.
- [ ] The lost-wakeup schedule passes at least 1,000 iterations.
- [ ] Cancellation is tested after staging and after acceptance.
- [ ] Partial transition failure is tested through `ReloadManager.reload()`.
- [ ] Actual commit-call exception handling is covered.
- [ ] Every failure class asserts candidate and old-generation close counts.
- [ ] Post-acceptance failure retry is proven idempotent.
- [ ] The focused CI job runs on Python 3.11 and 3.12.
- [ ] The exact implementation head has independently visible green CI evidence.

---

# Expected file targets

Primary production files:

- `src/eggpool/runtime_manager.py`
  - condition-backed lease waiting;
  - explicit gate state;
  - synchronized notifications;
  - removal of event clear/set race;
  - waiter accounting;
  - defensive gate-state validation API if required.

- `src/eggpool/control/reload_manager.py`
  - result ownership before transition apply;
  - expanded precommit cleanup owner;
  - explicit preacceptance/post-acceptance split;
  - accepted-finalization lifecycle;
  - cancellation handling at both sides of acceptance;
  - removal of direct runtime gate mutation.

- `src/eggpool/reload_transaction.py`
  - explicit acceptance and SQLite-commit facts;
  - transition rollback outcome;
  - accepted-finalization progress;
  - corrected concrete transition rollback state flags.

- `src/eggpool/db/connection.py`
  - actual commit-call wrapper;
  - typed commit error;
  - rollback-confirmed versus indeterminate outcome;
  - connection invalidation/recovery.

- `src/eggpool/reload_diagnostics.py`
  - cleanup degradation;
  - persistence outcome;
  - acceptance fact;
  - pending finalization steps;
  - connection invalidation status.

Likely test files:

- `tests/unit/test_runtime_manager.py`
- `tests/unit/test_process_transition_plan.py`
- `tests/unit/test_reload_manager.py`
- `tests/unit/test_reload_diagnostics_matrix.py`
- `tests/unit/test_db.py`
- `tests/integration/reload/test_pending_swap_visibility.py`
- `tests/integration/reload/test_sqlite_commit_failure.py`
- `tests/integration/reload/test_reload_fault_matrix.py`
- new focused files where isolation improves clarity, such as:
  - `tests/integration/reload/test_plan_017_lease_condition.py`;
  - `tests/integration/reload/test_plan_017_transition_cleanup.py`;
  - `tests/integration/reload/test_plan_017_acceptance_finalization.py`.

Verification/docs:

- `.github/workflows/ci.yml`
- `AGENTS.md`
- `.opencode/skills/development/SKILL.md`
- architecture skill only if the authoritative acceptance/condition contract needs documentation.

Do not modify unrelated request-path, provider, dashboard, or transcoder files.

---

# Implementation sequence

## Milestone 1 — Lease condition and lost-wakeup closure

1. Introduce condition-backed admission state.
2. Rewrite `acquire()` as predicate wait plus atomic claim.
3. Update stage, commit, rollback, install, and shutdown notifications.
4. Remove external gate mutation.
5. Add distinct-generation and deterministic barrier tests.
6. Run the 1,000-iteration schedule.

Exit criterion: no lost-wakeup path remains and all lease waiter accounting tests pass.

## Milestone 2 — Transition and candidate cleanup ownership

1. Retain `TransitionApplyResult` before apply.
2. Correct concrete transition rollback flags.
3. Expand structured rollback outcome.
4. Make `_abort_precommit_reload()` own candidate abort.
5. Remove duplicate outer cleanup policy.
6. Add production partial-apply and close-count tests.

Exit criterion: every preacceptance failure restores or explicitly degrades all reversible state and closes candidate resources exactly once.

## Milestone 3 — Acceptance and finalization separation

1. Add explicit SQLite commit and acceptance facts.
2. Split preacceptance and post-acceptance exception boundaries.
3. Add idempotent finalization record.
4. Correct ordinary exception and cancellation handling after acceptance.
5. Add persistence-committed/runtime-pending classification.
6. Add post-acceptance injection and retry tests.

Exit criterion: no accepted path can invoke rollback or candidate abort, and every finalization step is retryable without duplicate effects.

## Milestone 4 — Actual commit exception recovery

1. Wrap actual commit call.
2. Add typed recovery outcome.
3. Attempt and verify rollback.
4. Invalidate ambiguous connections.
5. Add confirmed rollback and indeterminate tests.
6. Verify subsequent transaction usability after confirmed recovery.

Exit criterion: actual commit exceptions are handled explicitly and never masquerade as the pre-call bypass case.

## Milestone 5 — Final verification and evidence

1. Run focused Python 3.11 and 3.12 suite.
2. Run full non-live suite.
3. Run format, lint, typecheck, and skip audit.
4. Run deterministic 1,000-iteration lease schedule.
5. Confirm no reload-specific exemption was reintroduced.
6. Capture exact-head CI evidence.

Exit criterion: every plan acceptance item is checked against the exact implementation commit.

---

# Required verification commands

The implementer may adjust filenames if tests are organized differently, but equivalent coverage is mandatory.

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run python scripts/audit_xfail_skips.py
```

Focused correctness suite:

```bash
uv run pytest \
  tests/unit/test_runtime_manager.py \
  tests/unit/test_process_transition_plan.py \
  tests/unit/test_reload_manager.py \
  tests/unit/test_reload_diagnostics_matrix.py \
  tests/unit/test_db.py \
  tests/integration/reload/test_pending_swap_visibility.py \
  tests/integration/reload/test_sqlite_commit_failure.py \
  tests/integration/reload/test_reload_fault_matrix.py \
  tests/integration/reload/test_plan_017_lease_condition.py \
  tests/integration/reload/test_plan_017_transition_cleanup.py \
  tests/integration/reload/test_plan_017_acceptance_finalization.py \
  -v
```

Deterministic race schedule:

```bash
uv run pytest \
  tests/integration/reload/test_plan_017_lease_condition.py \
  -k "lost_wakeup or publication_barrier" \
  --count=1000 \
  -q
```

If `pytest-repeat` is not a dependency, implement a bounded loop inside the deterministic test or add a repository script. Do not add a dependency solely for this command when a simple loop is sufficient.

Full repository gate:

```bash
uv run pytest -m "not live"
```

Use the repository's existing CI marker partition if the complete non-live suite requires separate performance or soak jobs.

---

# Final closure checklist

## Runtime synchronization

- [ ] Admission waiting has no event clear/set lost-wakeup window.
- [ ] Claim and lease increment are atomic with publication.
- [ ] Commit, rollback, install, and shutdown notify under the condition lock.
- [ ] No external component directly clears runtime-manager gate state.
- [ ] Waiter count returns to zero on every terminal path.

## Transition and candidate cleanup

- [ ] Partial transition result remains owned after apply failure.
- [ ] Applied prefix rolls back in reverse order.
- [ ] Rollback failure is not marked restored.
- [ ] Shared precommit cleanup owns candidate abort.
- [ ] Candidate closes exactly once before acceptance.
- [ ] Candidate never closes after acceptance.

## Acceptance and finalization

- [ ] Acceptance requires confirmed SQLite commit and runtime swap commit.
- [ ] Preacceptance and post-acceptance exception blocks are separate.
- [ ] Accepted paths cannot roll back runtime or process transitions.
- [ ] Post-acceptance finalization is idempotent and resumable.
- [ ] Cancellation after acceptance cannot revert or abandon the candidate.
- [ ] Retirement scheduling cannot be silently lost.
- [ ] Persistence-committed/runtime-pending is explicitly degraded.

## Database commit recovery

- [ ] Actual commit-call exceptions enter a dedicated handler.
- [ ] Confirmed rollback is distinguished from indeterminate outcome.
- [ ] Ambiguous connection state is invalidated.
- [ ] Candidate is never published after unconfirmed commit outcome.
- [ ] Subsequent transaction succeeds after confirmed rollback recovery.

## Verification

- [ ] Distinct-generation visibility tests pass.
- [ ] 1,000-iteration lost-wakeup schedule passes.
- [ ] Staged and post-acceptance cancellation barriers pass.
- [ ] Production partial-transition tests pass.
- [ ] Resource close-count assertions pass.
- [ ] Python 3.11 focused CI passes.
- [ ] Python 3.12 focused CI passes.
- [ ] Full non-live suite passes.
- [ ] Ruff format and lint pass.
- [ ] Pyright passes.
- [ ] Skip/xfail audit passes.
- [ ] Exact implementation SHA has visible green CI evidence.

---

# Definition of done

This line of work is closed only when all of the following are true:

1. A lease waiter cannot miss commit or rollback notification.
2. A partial transition apply failure restores the successfully applied prefix through the real reload path.
3. Every preacceptance failure closes candidate resources exactly once and reopens admission.
4. Every accepted reload keeps the candidate authoritative regardless of later exception or cancellation.
5. Post-acceptance housekeeping is idempotent, retryable, and diagnostically visible.
6. An exception from the actual SQLite commit call is recovered or classified indeterminate through a typed contract.
7. No test uses identical old/candidate generation identity to claim publication-race coverage.
8. The exact implementation head passes all focused and repository gates on supported Python versions.

If any item remains unproven, keep the plan open and record the missing evidence rather than marking reload atomicity complete.
