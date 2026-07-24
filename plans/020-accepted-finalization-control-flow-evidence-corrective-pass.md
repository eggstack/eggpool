# Accepted-Finalization Control-Flow and Evidence Corrective Pass

Date: 2026-07-24
Status: completed (2026-07-24)

Depends on:

- `plans/018-reload-atomicity-closure-corrective-pass.md`
- `plans/019-accepted-finalization-lifecycle-closure.md`

Implementation baseline:

- `73e27067dccdd966b96e0cf9f9d214de6ec8fefc`

## Objective

Close the remaining correctness and verification gaps in the accepted-reload finalization lifecycle after Plan 019.

Plan 019 materially improved the design:

- finalization progress and health are separate;
- only `COMPLETED` is terminal;
- transition finalization checks `TransitionFinalizeOutcome.remaining`;
- retirement fault injection reaches the real retirement call;
- completed jobs move into bounded scalar-only history;
- operational references are released after completion;
- unresolved finalization blocks new reload admission;
- shutdown drains finalization and can adopt an old slot retained by a committed swap.

Those changes must be preserved.

This corrective pass is required because the current implementation and evidence still do not satisfy Plan 019's own closure criteria:

1. `txn.mark_accepted()`, finalization-job construction, and job registration remain inside a rollback-capable `try` whose handlers call `_abort_precommit_reload()`.
2. `observer.on_retirement_started()` is awaited after acceptance but outside the retained finalization executor and outside a safe non-authoritative wrapper.
3. `AcceptedReloadFinalizationJob.run()` uses a lock but does not retain or share one process-owned execution task.
4. Cancelling or timing out a waiter can cancel the actual finalization attempt.
5. The post-acceptance cancellation path explicitly cancels the supposedly process-owned task.
6. An unknown finalization step is converted into `COMPLETED` rather than failing closed.
7. Retry counters count failures as retries and do not reconcile delayed completion.
8. Every finalization failure is currently capable of incrementing the retirement retry counter.
9. Successful retry does not clear active error fields or preserve a distinct bounded last-failure record.
10. `ReloadDiagnosticResult` omits the finalization fields accepted by `_finalize_reload()` and exposed by `ReloadResult`.
11. Accepted-but-unfinalized reloads are still classified as ordinary `SUCCESS_COMMITTED` results.
12. Application shutdown drains jobs without first waiting for an active reload transaction to leave its critical section.
13. The database invalidation tests still allow either rollback or indeterminate outcomes.
14. The mock ownership fallback still compares uppercase strings rather than canonical lowercase values.
15. Several tests described as production-path, close-once, persistent-failure, or weak-reference tests do not actually prove those claims.
16. The exact-head evidence file is manually asserted and does not match the repository's final documentation head.

This plan is deliberately narrow. It does not redesign live reload, runtime generations, transition planning, or the pending-swap protocol. It corrects the remaining control-flow, ownership, diagnostic, accounting, and evidence defects around the existing accepted-finalization architecture.

---

# Scope

## In scope

- structural separation of preacceptance rollback handling from accepted execution;
- creation and registration of accepted-finalization ownership without an exception window;
- safe handling of all post-acceptance observers and event recording;
- actual retained-task single-flight semantics;
- cancellation- and timeout-safe waiting on finalization attempts;
- fail-closed handling of invalid finalization progress;
- truthful attempt, failure, retry, retirement-retry, and completion counters;
- stale-error clearing with bounded prior-failure diagnostics;
- canonical finalization fields in internal diagnostics, snapshots, control responses, and events;
- accepted-but-pending result classification;
- shutdown ordering around active reload transactions;
- deterministic database invalidation tests;
- canonical lowercase candidate ownership fallback;
- production-path transition-prefix rollback tests;
- exact old-generation and active-generation close-once tests;
- real weak-reference retention tests;
- exact-head CI and evidence requirements.

## Explicit non-goals

Do not:

- redesign config parsing, validation, or disposition policy;
- broaden which configuration fields are live-reloadable;
- replace `RuntimeManager`, `PendingGenerationSwap`, generation leases, or SQLite;
- change provider routing, request dispatch, transcoding, metrics collection, or dashboard layout;
- introduce distributed or crash-persistent reload transactions;
- add a generic background worker framework;
- make observer delivery authoritative for accepting a generation;
- preserve compatibility with incorrect uppercase mock ownership strings;
- mark Plan 019 or Plan 020 complete based only on unit-level substitutes for production-boundary tests;
- modify unrelated provider, model catalog, pricing, dashboard, or network code.

---

# Mandatory invariants

## Acceptance-boundary invariants

1. The rollback-capable region ends before `txn.mark_accepted()` returns control to postacceptance code.
2. No `except` block lexically governing `txn.mark_accepted()` may unconditionally invoke `_abort_precommit_reload()`.
3. All objects required to retain accepted ownership are allocated before acceptance wherever possible.
4. Registration of the accepted-finalization owner cannot fail after acceptance because of ordinary allocation, dictionary mutation, logging, observer, or event code.
5. If an exceptional interpreter-level condition occurs after the runtime swap commits but before normal registration, one deterministic recovery function registers or adopts ownership without calling precommit cleanup.
6. Once `txn.reload_accepted` is true, no path may call:
   - `_abort_precommit_reload()`;
   - `pending_swap.rollback()`;
   - `transition_result.rollback_applied()`;
   - `candidate.abort()`;
   - `txn.mark_aborting()`;
   - `txn.mark_aborted()`.
7. Every typed and generic outer handler checks `txn.reload_accepted` before any abort transition.
8. Postacceptance observers and event writes are either:
   - finalization-job steps; or
   - safe, non-authoritative calls that cannot prevent job execution.
9. `observer.on_retirement_started()` cannot strand an accepted reload before its retained finalization job runs.

## Retained execution invariants

10. Every unresolved job has at most one retained execution task.
11. Concurrent callers await the same retained attempt rather than serially launching separate attempts.
12. Attempt count increments once per actual attempt task, not once per waiter.
13. Cancelling a waiter does not cancel the retained attempt.
14. Timing out admission or shutdown waiting does not cancel the retained attempt.
15. Explicit process shutdown may cancel a retained task only after ownership is deterministically transferred to shutdown cleanup.
16. A completed retained task is cleared before a later retry task is created.
17. A failed retained task leaves the progress cursor at the failed step and publishes a retry-pending outcome.
18. An invalid or unknown progress state raises an invariant error and remains unresolved; it never becomes `COMPLETED`.

## Accounting invariants

19. `attempt_count` counts actual finalization attempts.
20. `failure_count` counts failed attempts.
21. `retry_attempt_count` counts attempts after the first, whether they succeed or fail.
22. A first-attempt failure is not itself counted as a retry.
23. `retirement_retry_count` increments only when an actual retry attempt starts at `RETIREMENT_SCHEDULING`.
24. `fully_finalized_reloads` increments exactly once when a previously accepted job first reaches `COMPLETED`, regardless of whether completion happens inline, during admission, during explicit drain, or during shutdown drain.
25. Completion reconciliation is idempotent and cannot double-count when two waiters observe the same completion.
26. Active error fields are cleared after successful recovery.
27. Prior failure information, when retained, is stored separately as bounded immutable diagnostic data.
28. `last_failed_step` always identifies a failed step, never the last successful step.

## Diagnostic invariants

29. `ReloadDiagnosticResult` includes finalization status, next pending step, attempt count, failure count, retry-attempt count, and last active error fields.
30. `ReloadResult`, `ReloadDiagnosticResult`, reload-manager snapshots, control responses, and terminal operational events agree on finalization status.
31. Accepted and fully finalized is distinguishable from accepted and retry-pending.
32. Accepted and retirement-pending is distinguishable from other finalization failures.
33. Accepted-but-pending results use `POST_COMMIT_FINALIZATION_PENDING` or `RETIREMENT_SCHEDULE_FAILED`, not `SUCCESS_COMMITTED`.
34. `SUCCESS_COMMITTED` means the reload is accepted and all required accepted-finalization steps completed.
35. Diagnostics derive persistence and transition facts from the transaction object, not unconditional literals.
36. Completed history does not present stale active errors.

## Shutdown invariants

37. Shutdown stops new control requests first.
38. Shutdown waits for the active reload transaction to reach a safe terminal or accepted-owned state before draining jobs.
39. The transaction wait is bounded and produces explicit diagnostics on timeout.
40. Finalization drain starts only after the transaction wait.
41. Runtime shutdown starts only after finalization drain or explicit ownership adoption.
42. If the active transaction cannot finish before the bound, shutdown adopts all committed runtime ownership that can no longer remain under the reload coroutine.
43. The exact old generation retained by a committed unresolved swap closes exactly once.
44. The active generation closes exactly once.
45. A slot already scheduled for retirement is not scheduled a second time by shutdown adoption.

## Verification invariants

46. Tests claiming `ReloadManager.reload()` coverage must invoke that method through the real integration harness.
47. Tests claiming SQLite atomicity must cross the actual `Database.transaction()` boundary.
48. Tests claiming staged-swap rollback must assert the real `PendingGenerationSwap` state and active generation identity.
49. Tests claiming candidate cleanup must instrument actual candidate resources and assert exact close counts.
50. Tests claiming weak-reference collection must create `weakref.ref` objects and assert they become `None` after pruning and garbage collection.
51. Tests claiming shutdown adoption must keep finalization unresolved through drain, then prove shutdown closes the exact retained old slot.
52. Tests claiming close-once must assert per-generation resource close counters, not merely lease rejection.
53. Tests claiming deterministic indeterminate commit must force the indeterminate branch and assert connection detachment and typed follow-up failure.
54. Exact-head evidence must identify the full implementation SHA and be backed by repository-visible CI status or archived machine output tied to that SHA.

---

# Workstream A — Structurally close the acceptance boundary

## A1. Split commit into preacceptance and accepted phases

Refactor the commit path into explicit functions or lexical regions:

```python
commit_result = await self._commit_preacceptance(...)
accepted_owner = self._accept_commit(commit_result)
return await self._run_accepted_phase(accepted_owner, ...)
```

The exact names may differ, but the separation is mandatory.

`_commit_preacceptance()` may own:

- the SQLite transaction;
- pending-swap staging;
- process-transition application;
- SQLite commit outcome handling;
- runtime-swap commit;
- preacceptance rollback.

It must return only after the SQLite transaction and runtime swap have committed.

The accepted phase may own:

- `txn.mark_accepted()`;
- finalization-owner registration;
- finalization execution;
- accepted diagnostics;
- safe observers and event recording.

No preacceptance cleanup handler may govern the accepted phase.

## A2. Prepare ownership before flipping acceptance

Create a lightweight accepted-finalization context before the final acceptance marker where practical. It should contain references to:

- transaction;
- candidate;
- pending swap;
- transition result;
- published generation;
- old generation ID;
- observer and app targets;
- request and generation identifiers.

Then perform a minimal non-awaiting acceptance sequence:

1. runtime swap commit returns;
2. accepted context is complete;
3. `txn.mark_accepted()`;
4. finalization job is inserted into the active registry;
5. control leaves the preacceptance region.

Dictionary insertion must not be mixed with observers, event writes, logging formatters, or arbitrary user callbacks.

If strict no-failure registration cannot be guaranteed, introduce one small `_ensure_accepted_owner_registered()` recovery function. Every accepted exception path calls it before doing anything else.

## A3. Move postacceptance observer work into safe ownership

`observer.on_retirement_started()` must not remain an unprotected await between accepted registration and `job.run()`.

Choose one of:

- make it an explicit finalization step before retirement scheduling; or
- invoke it through a safe observer wrapper inside the finalization executor.

Required behavior:

- observer failure is logged and diagnosed;
- observer failure does not block transition finalization or retirement;
- observer failure cannot cause an accepted transaction to enter a typed preacceptance handler;
- observer failure cannot prevent the job from remaining registered.

## A4. Harden all outer handlers

Before any typed handler calls `txn.mark_aborting()` or returns a preacceptance diagnostic, it must branch on `txn.reload_accepted`.

At minimum audit:

- `ReloadPreparationError`;
- `ReloadReconciliationError`;
- `DatabaseCommitError` and translated commit errors;
- `asyncio.CancelledError`;
- generic `Exception`;
- observer-specific exceptions if any remain outside safe wrappers.

For accepted transactions, handlers may only:

- ensure ownership registration;
- publish accepted/pending diagnostics;
- shield or await retained finalization according to policy;
- return or propagate cancellation without rollback.

## A5. Add a source-level structural guard

Add a focused test or small AST/source audit that fails if:

- `txn.mark_accepted()` is inside the same `try` body as an `except` that invokes `_abort_precommit_reload()`; or
- an accepted-path observer await exists before owner registration.

This is supplementary. It does not replace runtime tests.

### Acceptance criteria — Workstream A

- [ ] `txn.mark_accepted()` is outside the rollback-capable inner `try`.
- [ ] Finalization ownership is registered before any postacceptance await.
- [ ] `on_retirement_started` is safe and non-authoritative.
- [ ] Every outer typed handler checks `txn.reload_accepted` before abort logic.
- [ ] An injected exception immediately after acceptance produces zero rollback, transition rollback, candidate abort, `mark_aborting`, and `mark_aborted` calls.
- [ ] A postacceptance `ReloadPreparationError` from an observer cannot corrupt the transaction state.
- [ ] The accepted generation remains authoritative and the retained job remains available after every injected postacceptance fault.

---

# Workstream B — Implement real retained-task single-flight

## B1. Use `_run_task` as the process-owned attempt

Replace lock-only execution with retained-task execution.

A suitable pattern is:

```python
async def run(self) -> AcceptedFinalizationOutcome:
    async with self._run_lock:
        if self.is_complete:
            return self._completed_outcome()
        task = self._run_task
        if task is None or task.done():
            task = asyncio.create_task(self._run_attempt())
            self._run_task = task
    return await asyncio.shield(task)
```

The implementation must account for a completed failed task before creating the next retry attempt.

Do not hold `_run_lock` while awaiting the entire attempt.

## B2. Return a structured outcome

`run()` should return a frozen scalar-only outcome such as:

```python
@dataclass(frozen=True)
class AcceptedFinalizationOutcome:
    completed: bool
    next_step: str | None
    attempt_count: int
    failure_count: int
    retry_attempt_count: int
    failed_step: str | None
    error_class: str | None
    error_message: str | None
    retry_permitted: bool
```

Returning only an enum step is insufficient for manager-level reconciliation.

## B3. Make waiters cancellation-safe

All callers must await the retained task through `asyncio.shield()`.

Audit and correct:

- inline accepted execution;
- new-reload admission retry;
- explicit `drain_finalization_jobs()`;
- application shutdown drain;
- postacceptance cancellation recovery.

`asyncio.wait_for(job.run(), ...)` is acceptable only if `job.run()` shields the retained task internally or the caller shields the retained task explicitly.

A waiter timeout returns control to the caller but leaves the attempt running.

## B4. Remove cancellation of retained attempts

The postacceptance cancellation path must not call `critical_task.cancel()` merely because the caller's bound expired.

Cancellation is permissible only during deterministic process shutdown after:

- the job's pending-swap ownership is adopted by runtime shutdown; and
- cancellation cannot orphan the old slot or candidate resources.

## B5. Fail closed on invalid progress

Replace the current unknown-step behavior that marks the job complete.

Required behavior:

- raise a typed `AcceptedFinalizationInvariantError`;
- leave the current step unchanged;
- set health to retry-pending or invariant-failed;
- keep the job in the active registry;
- expose the invariant failure in diagnostics;
- block new reload admission until explicit recovery or shutdown adoption.

### Acceptance criteria — Workstream B

- [ ] Concurrent callers share one task object and one attempt count increment.
- [ ] A second waiter does not trigger an immediate second attempt after the first attempt fails while both were waiting on the same task.
- [ ] Cancelling one waiter does not cancel the attempt.
- [ ] Timing out admission waiting does not cancel the attempt.
- [ ] Timing out shutdown drain does not cancel the attempt before ownership adoption.
- [ ] `_run_task` is cleared or replaced only under the single-flight lock.
- [ ] Unknown progress remains unresolved and never becomes completed.

---

# Workstream C — Correct counters, error lifecycle, and completion reconciliation

## C1. Define exact counter semantics

Use distinct job-level fields:

- `attempt_count`;
- `failure_count`;
- `retry_attempt_count`;
- `retirement_retry_attempt_count`.

Rules:

- first attempt: `attempt_count += 1`, retry count unchanged;
- first failure: `failure_count += 1`, retry count unchanged;
- second attempt: `attempt_count += 1`, `retry_attempt_count += 1`;
- an attempt starting at retirement scheduling additionally increments `retirement_retry_attempt_count` when it is a retry;
- success does not erase aggregate counts.

## C2. Centralize manager reconciliation

Create one idempotent manager function, for example:

```python
def _reconcile_finalization_job(self, job, outcome) -> None:
    ...
```

It should own:

- failure and retry counter deltas;
- fully-finalized counter transition;
- retirement retry deltas;
- active-registry removal;
- history append;
- reference release;
- last diagnostic update where appropriate.

Call it after every job observation path:

- inline accepted run;
- admission retry;
- explicit drain;
- shutdown drain;
- background retained-task completion callback if used.

Store per-job accounted values or an accounting generation so repeated reconciliation is idempotent.

## C3. Reconcile delayed completion

When a retry completes a job after the original reload returned `retry_pending`:

- increment `fully_finalized_reloads` exactly once;
- archive the final completion record;
- remove the active job;
- release references;
- update unresolved count;
- emit a finalization-completed operational event tied to the original request ID.

Do not retroactively change the original immediate response, but update canonical manager diagnostics/history.

## C4. Clear active errors after recovery

On successful completion after failure:

- active `error_class`, `error_message`, and `failed_step` become `None`;
- retain immutable prior-failure summary separately if desired;
- `last_failed_step` in history reflects the actual failed step;
- the successful final step must not overwrite it.

A bounded attempt history may include:

- attempt number;
- starting step;
- outcome;
- failure class/message;
- timestamps.

It must contain scalars only.

### Acceptance criteria — Workstream C

- [ ] First-attempt failure does not increment retry-attempt counters.
- [ ] The first real retry increments retry-attempt count once.
- [ ] Only retirement-step retries increment retirement retry count.
- [ ] Delayed completion increments `fully_finalized_reloads` exactly once.
- [ ] Two concurrent reconciliations cannot double-count completion.
- [ ] Successful recovery clears active error fields.
- [ ] Completed history retains truthful prior-failure data without active error ambiguity.

---

# Workstream D — Make diagnostics canonical and consistent

## D1. Extend `ReloadDiagnosticResult`

Add frozen fields:

- `finalization_status`;
- `finalization_next_step`;
- `finalization_attempt_count`;
- `finalization_failure_count`;
- `finalization_retry_attempt_count`;
- `finalization_last_error_step`;
- `finalization_last_error_class`;
- `finalization_last_error_message`;
- `pending_swap_committed`;
- `accepted_generation_authoritative` if not already derivable.

Populate these in `_finalize_reload()` rather than accepting and discarding them.

## D2. Classify pending accepted outcomes correctly

Update result classification so:

- fully finalized accepted reload → `SUCCESS_COMMITTED`;
- accepted with non-retirement finalization pending → `POST_COMMIT_FINALIZATION_PENDING`;
- accepted with retirement scheduling pending → `RETIREMENT_SCHEDULE_FAILED` or an equivalent stable accepted-degraded category.

These categories are operational states, not preacceptance failures. Preserve `ok=True` if the API contract defines `ok` as accepted/authoritative.

## D3. Propagate fields through every surface

Audit and update:

- `ReloadResult`;
- `ReloadDiagnosticResult`;
- `ReloadManager.snapshot()`;
- bounded reload history;
- finalization history;
- control protocol response model and handler;
- CLI rendering for `rehash`, `connect`, and other reload callers as applicable;
- operational terminal events;
- readiness or health detail if unresolved finalization affects operator status.

Do not expose exception tracebacks, raw configs, credentials, or live objects.

## D4. Derive facts from the transaction

Replace unconditional literals passed to `_finalize_reload()` where they can be false or premature.

At minimum derive:

- `persistence_committed` from `txn.persistence_committed` or the precise accepted database fact;
- `process_transitions_applied` from `txn.process_transitions_applied`;
- retirement scheduling from transaction/finalization state;
- pending step from job outcome.

Document the distinction between:

- transitions applied;
- transition snapshots finalized;
- transaction fully finalized.

### Acceptance criteria — Workstream D

- [ ] Canonical diagnostics contain every finalization field.
- [ ] Immediate response, manager snapshot, history, and control response agree.
- [ ] Retry-pending accepted reload is not classified as ordinary success committed.
- [ ] Retirement-pending accepted reload is specifically identifiable.
- [ ] Delayed completion updates canonical history and emits a completion event.
- [ ] Transaction facts are not supplied as unconditional `True` values.

---

# Workstream E — Correct shutdown ordering and adoption

## E1. Wait for the active transaction

In application shutdown, after stopping the control server and before draining finalization jobs:

```python
completed = await reload_manager.wait_for_transaction_completion(timeout_s=...)
```

Required interpretation:

- no active transaction → proceed;
- preacceptance transaction completes/aborts → proceed;
- accepted transaction registers its owner and leaves the reload coroutine → proceed;
- timeout → capture transaction snapshot and invoke explicit shutdown ownership recovery.

Do not proceed directly from control-server stop to finalization drain while a transaction may still be between runtime commit and owner registration.

## E2. Add explicit shutdown ownership recovery

If transaction waiting times out, inspect:

- `txn.reload_accepted`;
- pending swap state;
- active generation identity;
- candidate ownership state;
- existing finalization registry.

Then ensure one owner exists for every committed accepted swap before runtime shutdown.

This recovery must not attempt preacceptance rollback for an accepted transaction.

## E3. Preserve retained tasks during bounded drain

A drain timeout should stop waiting, not cancel the retained finalization attempt.

Before runtime shutdown adopts a committed old slot:

- ensure any still-running job cannot later schedule duplicate retirement;
- atomically mark ownership as adopted or make pending-swap finalization idempotently observe shutdown ownership;
- prevent a late job completion from double-closing the slot.

## E4. Prove shutdown close-once behavior

Instrument old and active generation resources with distinct counters.

Persistent-failure test must:

1. create a committed unresolved retirement job;
2. keep retirement failure active through finalization drain;
3. verify the old slot remains owned by the committed swap;
4. invoke application or runtime shutdown;
5. assert exact old generation ID closed once;
6. assert exact active generation ID closed once;
7. assert no duplicate retirement task exists;
8. assert the pending swap cannot later close either generation again.

### Acceptance criteria — Workstream E

- [ ] Shutdown waits for an active reload transaction before job drain.
- [ ] Timeout produces explicit ownership recovery rather than a race.
- [ ] Drain timeout does not cancel a retained attempt prematurely.
- [ ] Shutdown adoption and late finalization are mutually idempotent.
- [ ] Exact old and active generation resources each close once.
- [ ] Database and other process-owned dependencies close after reload/runtime ownership is resolved.

---

# Workstream F — Replace weak or misleading tests with production-boundary proof

## F1. Acceptance-window fault matrix

Add exact fault seams at these boundaries:

1. immediately before `txn.mark_accepted()`;
2. immediately after `txn.mark_accepted()`;
3. immediately after owner registration;
4. retirement-start observer;
5. transition finalization;
6. retirement scheduling;
7. transaction completion bookkeeping.

For each postacceptance fault assert:

- active generation is the candidate generation;
- SQLite accepted state remains committed;
- candidate abort count is zero;
- transition rollback count is zero;
- pending-swap rollback count is zero;
- transaction never enters aborting/aborted;
- one unresolved owner remains registered unless completion succeeded.

The immediate-after-acceptance seam should become structurally unreachable or recoverable without cleanup.

## F2. Real single-flight tests

Use a blocking real finalization step and capture the retained task identity.

Prove:

- two callers receive the same attempt result;
- the step body executes once;
- attempt count increments once;
- cancelling caller A does not cancel caller B or the attempt;
- timing out caller A leaves the attempt running;
- after a failed shared attempt, a later explicit call creates exactly one new retry task.

## F3. Production A/B/C transition-prefix rollback

Inject an actual `ProcessTransitionPlan` into `ReloadManager.reload()` through the integration harness:

- A applies successfully;
- B fails;
- C is untouched.

Assert across the full path:

- SQLite transaction rolls back;
- staged pending swap rolls back;
- old generation remains active;
- lease admission is open;
- A rollback executes once;
- B and C rollback do not execute;
- candidate resources close once;
- no finalization job is registered;
- a subsequent reload succeeds.

Directly calling `TransitionApplyResult.apply_all()` is useful unit coverage but does not satisfy this closure test.

## F4. Deterministic database outcome matrix

Provide separate seams/tests for:

1. commit failure + confirmed rollback success;
2. commit failure + rollback failure;
3. commit failure + externally ambiguous `in_transaction=False` state;
4. connection close/detach failure if relevant.

For the indeterminate test assert unconditionally:

- `err.outcome == "indeterminate"`;
- `err.connection_invalidated is True`;
- database internal connection reference is detached;
- invalidation flag is set;
- subsequent transaction raises `DatabaseConnectionInvalidatedError`;
- reconnection is required before use.

Tests that accept either `rolled_back` or `indeterminate` do not satisfy this workstream.

## F5. Canonical ownership fallback

Correct the production fallback to normalize:

```python
state_value = getattr(candidate_state, "value", candidate_state)
```

Then compare against canonical lowercase values:

- `"transferred"`;
- `"aborted"`.

Production-path tests must call `_abort_precommit_reload()` with mock candidates representing:

- lowercase transferred;
- lowercase aborted;
- building/prepared;
- actual enum values.

Transferred and aborted candidates must not be aborted again.

## F6. Real retention proof

Enhance the repeated-reload test to:

- capture weak references to candidate, pending swap, transaction, published generation, and old generation resources from selected iterations;
- run enough alternating reloads to exceed history capacity;
- force garbage collection;
- assert weak references are cleared after completion/history eviction where no other legitimate owner remains;
- assert active registry is empty;
- assert history remains bounded;
- assert each retired resource closes once.

## F7. Real shutdown adoption proof

Do not clear a one-shot seam before drain and then claim persistent failure.

Use a persistent seam or patch the production retirement call so failure remains active through drain. Clear or bypass it only after shutdown ownership has been adopted, if needed to permit cleanup.

Assert exact slot identity and close counters.

### Acceptance criteria — Workstream F

- [ ] Acceptance-window matrix crosses the real reload path.
- [ ] Single-flight tests prove task identity and cancellation isolation.
- [ ] A/B/C test crosses SQLite, staged swap, rollback, candidate cleanup, and reload admission.
- [ ] Indeterminate DB invalidation is unconditional and deterministic.
- [ ] Production ownership fallback uses lowercase canonical values.
- [ ] Weak-reference tests assert actual collection.
- [ ] Shutdown tests assert exact per-generation close counts.
- [ ] Test names and documentation do not claim evidence broader than the assertions performed.

---

# Workstream G — CI and exact-head evidence

## G1. Focused CI matrix

Retain Python 3.11 and 3.12 coverage and include all Plan 018–020 reload lifecycle tests.

The focused command must include:

- finalization state machine;
- acceptance-window matrix;
- single-flight cancellation/timeout tests;
- production transition-prefix rollback;
- deterministic database outcomes;
- registry/weak-reference retention;
- shutdown transaction wait and adoption;
- diagnostics and counter reconciliation;
- ownership fallback.

## G2. Repeat race-sensitive tests

Run the following at least five times on each Python version:

- concurrent finalization callers;
- waiter cancellation;
- admission timeout while attempt continues;
- shutdown drain timeout while attempt continues;
- active transaction versus shutdown;
- delayed completion accounting.

No flaky retries, sleeps used as correctness barriers, or conditional assertions are acceptable.

Use explicit events/barriers.

## G3. Full repository verification

At the exact implementation head run:

- `ruff format --check`;
- `ruff check`;
- `pyright src/ scripts/`;
- full `pytest` suite;
- skip/xfail policy audit;
- relevant soak or repeated reload test if maintained separately.

## G4. Evidence artifact requirements

Write or update an evidence artifact only after the final code commit exists.

It must contain:

- full 40-character implementation SHA;
- commands executed;
- Python versions;
- pass/fail/skip/xfail counts;
- repeated-run counts;
- CI run IDs or status-check names when available;
- artifact identifiers for logs when available;
- explicit statement of any connector visibility limitation.

Do not call a documentation-only follow-up commit the tested exact implementation head unless checks were rerun on that commit.

Prefer one final evidence commit whose own CI verifies the evidence-file update, or record both:

- tested implementation SHA;
- evidence-document SHA.

### Acceptance criteria — Workstream G

- [ ] Focused 3.11/3.12 CI contains all Plan 020 tests.
- [ ] Race-sensitive tests pass five consecutive runs per Python version.
- [ ] Full repository checks pass at the recorded implementation SHA.
- [ ] Skip/xfail audit is clean.
- [ ] Evidence uses full SHAs and does not mislabel an earlier commit as current `HEAD`.
- [ ] Repository-visible checks or archived machine output support the evidence claims.

---

# Implementation sequence

## Milestone 1 — Control-flow and retained execution

Implement Workstreams A and B.

Exit gate:

- accepted code is structurally outside rollback handling;
- all postacceptance observers are safe;
- one retained task owns each attempt;
- cancellation and timeout affect waiters, not execution;
- invalid progress fails closed.

## Milestone 2 — Accounting and canonical diagnostics

Implement Workstreams C and D.

Exit gate:

- counter deltas are exact and idempotent;
- delayed completion is reconciled;
- stale errors clear after recovery;
- all diagnostic surfaces agree;
- pending accepted reloads are classified distinctly.

## Milestone 3 — Shutdown ordering and production proof

Implement Workstreams E and F.

Exit gate:

- shutdown waits for the active transaction;
- unresolved ownership is adopted without races;
- exact old/active close-once behavior is proven;
- database, transition, ownership, retention, and boundary tests cross production paths.

## Milestone 4 — Exact-head verification

Implement Workstream G.

Exit gate:

- focused and full checks pass on Python 3.11/3.12 as applicable;
- race-sensitive cases repeat cleanly;
- exact-head evidence is machine-supported;
- Plan 019 and Plan 020 status are updated only after all gates are satisfied.

---

# Expected file targets

Likely production targets:

- `src/eggpool/control/accepted_finalization.py`
- `src/eggpool/control/reload_manager.py`
- `src/eggpool/reload_transaction.py`
- `src/eggpool/reload_diagnostics.py`
- `src/eggpool/config_reload_policy.py`
- `src/eggpool/control/protocol.py` or the current control response model
- `src/eggpool/app.py`
- `src/eggpool/runtime_manager.py`
- `src/eggpool/db/connection.py`
- `src/eggpool/errors.py`
- `.github/workflows/ci.yml`

Likely test targets:

- `tests/unit/test_accepted_finalization_state_machine.py`
- `tests/integration/reload/test_plan_019_acceptance_boundary.py`
- `tests/integration/reload/test_plan_019_finalization_retry.py`
- `tests/integration/reload/test_plan_019_finalization_retention.py`
- `tests/integration/reload/test_plan_019_shutdown_drain.py`
- `tests/integration/reload/test_plan_019_shutdown_adoption.py`
- `tests/integration/reload/test_plan_019_database_invalidation.py`
- `tests/integration/reload/test_plan_019_transition_prefix.py`
- `tests/integration/reload/test_plan_019_diagnostics_assertions.py`
- new `tests/integration/reload/test_plan_020_acceptance_window.py`
- new `tests/integration/reload/test_plan_020_single_flight.py`
- new `tests/integration/reload/test_plan_020_shutdown_transaction_ordering.py`
- new `tests/integration/reload/test_plan_020_production_transition_rollback.py`
- new `tests/integration/reload/test_plan_020_database_outcome_matrix.py`
- new `tests/integration/reload/test_plan_020_retention_close_counts.py`
- new `tests/integration/reload/test_plan_020_diagnostics_reconciliation.py`

Evidence target:

- `artifacts/plan-020-evidence.md`

Do not modify unrelated provider, dashboard, router, transcoder, catalog, pricing, or request-dispatch modules.

---

# Implementation review checklist

## Acceptance boundary

- [ ] Accepted phase is outside rollback-capable `try` blocks.
- [ ] Owner registration precedes every postacceptance await.
- [ ] Postacceptance observers are safe and non-authoritative.
- [ ] Every typed handler checks acceptance first.
- [ ] Accepted fault matrix proves zero rollback/abort calls.

## Retained execution

- [ ] `_run_task` is the actual process-owned attempt.
- [ ] Concurrent waiters share one task.
- [ ] Waiter cancellation does not cancel execution.
- [ ] Waiter timeout does not cancel execution.
- [ ] Invalid progress fails closed.

## Accounting

- [ ] Attempts, failures, and retry attempts have distinct semantics.
- [ ] Retirement retries count only retirement retries.
- [ ] Delayed completion updates counters exactly once.
- [ ] Reconciliation is idempotent.
- [ ] Successful recovery clears active errors.

## Diagnostics

- [ ] Canonical diagnostic model contains finalization fields.
- [ ] Result, snapshot, history, control response, and events agree.
- [ ] Pending accepted categories are distinct from full success.
- [ ] Transaction facts are operation-derived.

## Shutdown

- [ ] Control server stops first.
- [ ] Active transaction is awaited.
- [ ] Jobs drain after transaction wait.
- [ ] Timeout invokes deterministic ownership recovery.
- [ ] Old and active generations close exactly once.

## Verification

- [ ] Production A/B/C rollback test crosses full reload path.
- [ ] DB invalidation outcomes are deterministic.
- [ ] Ownership fallback uses lowercase values in production.
- [ ] Weak references are actually asserted collectible.
- [ ] Persistent shutdown failure remains persistent through drain.
- [ ] Exact close counters are asserted.
- [ ] Exact-head CI evidence is machine-supported.

---

# Global closure gate

Plan 020 is complete only when every statement below is true:

1. `txn.mark_accepted()` is not governed by a preacceptance cleanup handler.
2. Finalization ownership is registered before any postacceptance await.
3. A postacceptance observer failure cannot prevent finalization execution.
4. Every accepted exception path performs zero swap rollback, transition rollback, candidate abort, or transaction abort transitions.
5. Concurrent finalization callers await one retained attempt task.
6. Cancelling or timing out a waiter does not cancel the retained attempt.
7. Unknown progress cannot be converted into successful completion.
8. Attempt, failure, retry, and retirement-retry counters have distinct truthful semantics.
9. Delayed completion increments fully-finalized accounting exactly once.
10. Successful recovery clears active error fields while retaining bounded truthful failure history.
11. Canonical diagnostics contain finalization status and agree across all exposed surfaces.
12. Accepted-but-pending outcomes are not classified as ordinary committed success.
13. Shutdown waits for the active reload transaction before finalization drain.
14. Shutdown timeout cannot orphan ownership between runtime commit and job registration.
15. Drain timeout does not cancel retained work before deterministic adoption.
16. The exact old generation retained by a committed unresolved swap closes once.
17. The exact active generation closes once.
18. The production A/B/C transition-prefix failure rolls back SQLite, staged swap, transition A, and candidate resources correctly.
19. Deterministic database tests force and prove confirmed rollback and indeterminate invalidation separately.
20. Production ownership fallback uses canonical lowercase values.
21. Repeated reload tests prove actual weak-reference collection and exact resource close counts.
22. Persistent shutdown-failure tests keep the failure active through drain and prove adoption.
23. Focused Python 3.11 and 3.12 tests pass repeatedly at the implementation head.
24. Full repository checks and skip/xfail audit pass at the implementation head.
25. Exact-head evidence uses full SHAs and is supported by CI status or archived machine output tied to those SHAs.

Until all 25 statements are evidenced, Plan 019 must not be considered fully closed and Plan 020 remains open.
