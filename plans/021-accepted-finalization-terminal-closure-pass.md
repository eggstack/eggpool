# Accepted-Finalization Terminal Closure Pass

Date: 2026-07-24
Status: implementation handoff

Depends on:

- `plans/018-reload-atomicity-closure-corrective-pass.md`
- `plans/019-accepted-finalization-lifecycle-closure.md`
- `plans/020-accepted-finalization-control-flow-evidence-corrective-pass.md`

Implementation baseline:

- `e3bd47c0cedb55449b7f73a87698b359adbd678a`

## Objective

Close the remaining accepted-reload finalization defects after Plan 020 and establish a defensible terminal verification boundary for this line of work.

Plan 020 materially improved the subsystem:

- accepted finalization now uses a retained process-owned `asyncio.Task`;
- callers await retained attempts through `asyncio.shield()`;
- invalid progress raises a typed invariant error;
- transition finalization honors `TransitionFinalizeOutcome.remaining`;
- postacceptance observers run as non-authoritative finalization work;
- finalization fields propagate through internal diagnostics, reload results, and control responses;
- accepted-but-pending reloads have distinct result categories;
- shutdown waits for an active transaction before draining finalization jobs;
- candidate ownership fallback uses canonical lowercase values.

Those changes must be preserved.

This closure pass is required because the current implementation and evidence still leave the following concrete defects:

1. `txn.mark_accepted()` remains lexically inside the rollback-capable `try` whose handlers invoke `_abort_precommit_reload()`.
2. `RETIREMENT_SCHEDULING` is declared but never entered, so retirement-specific status and retry accounting are effectively unreachable.
3. Accepted/committed counters are incremented only after inline finalization waiting, allowing postacceptance cancellation to suppress truthful acceptance accounting.
4. A retained finalization task may finish after its request waiter is cancelled without any process-owned completion reconciliation.
5. Admission only inspects unresolved jobs, so a completed-but-unreconciled job can remain indefinitely in the active registry with strong runtime references.
6. Pending failure accounting uses a one-time Boolean rather than per-counter deltas, causing later failed attempts to be undercounted.
7. `adopt_for_shutdown()` overloads the reference-release flag, does not expose a distinct adoption state, and can prevent later reference release.
8. `FinalizationStatus.SHUTDOWN_ADOPTED` exists but is not naturally emitted.
9. Shutdown ignores the Boolean result from `wait_for_transaction_completion()` and proceeds even when the active reload remains unsafe.
10. The production transition-prefix test does not construct real A/B/C transitions and fails before `TransitionApplyResult.apply_all()`.
11. Database outcome tests still permit multiple outcomes rather than forcing each rollback/indeterminate branch deterministically.
12. Weak-reference tests do not prove captured references become collectible.
13. Shutdown close-once tests do not assert instrumented per-generation close counts.
14. The evidence artifact does not identify the final full commit SHA or show a clean rerun after the two subsequent CI-test commits.

This plan is deliberately narrow. It does not redesign live reload, provider routing, runtime generation construction, SQLite persistence architecture, process transition semantics, or the control protocol. It closes the remaining lifecycle, accounting, shutdown, and evidence gaps around the existing accepted-finalization design.

---

# Scope

## In scope

- strict lexical separation of the acceptance transition from rollback-capable handlers;
- a real `RETIREMENT_SCHEDULING` progress transition;
- retirement-specific status and retry accounting;
- acceptance accounting at the acceptance boundary;
- process-owned completion reconciliation independent of request waiters;
- delta-based attempt, failure, retry, and retirement-retry accounting;
- active registry enforcement as unresolved-only;
- bounded immutable history after every completion path;
- distinct shutdown-adoption state and reference-release state;
- explicit shutdown behavior when transaction waiting times out;
- exact old-generation and active-generation close-once behavior;
- canonical diagnostic reconciliation after delayed completion or shutdown adoption;
- production-path A/B/C transition-prefix rollback tests;
- deterministic SQLite rollback and indeterminate-outcome tests;
- real weak-reference collection tests;
- repeated accepted-cancellation retention tests;
- exact-head CI and evidence artifacts.

## Explicit non-goals

Do not:

- alter which configuration fields are live-reloadable;
- redesign config parsing, validation, or diff classification;
- replace `RuntimeManager`, `PendingGenerationSwap`, generation leases, or SQLite;
- add distributed or crash-persistent reload transactions;
- introduce a general-purpose background job scheduler;
- make observers authoritative for acceptance or retirement;
- change provider dispatch, account selection, transcoding, metrics aggregation, or dashboard behavior;
- broaden the control protocol beyond the existing finalization fields unless required for truthful status values;
- weaken existing Plan 018, Plan 019, or Plan 020 tests to obtain green CI;
- mark this plan complete using unit-level substitutes for production-boundary assertions.

---

# Mandatory invariants

## Acceptance-boundary invariants

1. SQLite commit and runtime-swap commit remain the factual acceptance prerequisites.
2. `txn.mark_accepted()` executes outside every `try` whose handlers may call `_abort_precommit_reload()`.
3. No exception handler lexically governing `txn.mark_accepted()` may invoke:
   - `_abort_precommit_reload()`;
   - `pending_swap.rollback()`;
   - `transition_result.rollback_applied()`;
   - `candidate.abort()`;
   - `txn.mark_aborting()`;
   - `txn.mark_aborted()`.
4. Accepted-finalization ownership is fully prepared before the acceptance transition whenever possible.
5. The owner is registered synchronously immediately after acceptance and before the first postacceptance await.
6. A failure in owner registration after acceptance invokes one deterministic accepted-owner recovery path and never precommit cleanup.
7. Accepted/committed counters increment exactly once at the acceptance boundary, not after finalization waiting.
8. Cancellation or timeout after acceptance cannot erase or delay the acceptance accounting fact.

## Progress and status invariants

9. Finalization progress explicitly enters `RETIREMENT_SCHEDULING` before attempting `pending_swap.finalize_retirement()`.
10. A failure from `finalize_retirement()` leaves the cursor at `RETIREMENT_SCHEDULING`.
11. A successful retirement attempt advances from `RETIREMENT_SCHEDULING` to `RETIREMENT_SCHEDULED` exactly once.
12. `FinalizationStatus.RETIREMENT_SCHEDULE_FAILED` is emitted when the latest failure occurred at `RETIREMENT_SCHEDULING`.
13. `RETIREMENT_SCHEDULE_FAILED` is distinguishable from generic `RETRY_PENDING` in results, snapshots, history, and events.
14. An unknown progress state remains an invariant failure and never advances.
15. `COMPLETED` remains the only fully finalized terminal progress state.

## Reconciliation invariants

16. Finalization completion is reconciled by process-owned machinery, not by whichever request waiter happens to observe the task.
17. Every retained attempt has one completion callback or equivalent process-owned observer that schedules reconciliation.
18. Cancelling all request waiters does not suppress completion reconciliation.
19. Timing out admission or shutdown waiting does not suppress completion reconciliation.
20. Reconciliation is idempotent across inline, callback, admission, explicit drain, and shutdown paths.
21. A completed job cannot remain in the active registry after the event loop has had an opportunity to run its completion reconciliation.
22. The active registry contains unresolved operational jobs only.
23. Completed jobs are moved into bounded immutable scalar-only history.
24. Operational references are released after completion history is captured.
25. Repeated postacceptance cancellations do not produce unbounded registry growth or retained runtime objects.

## Accounting invariants

26. `accepted_reloads` counts accepted reloads exactly once at acceptance.
27. `committed_reloads` counts accepted reloads exactly once at acceptance.
28. `fully_finalized_reloads` counts first transition to finalization `COMPLETED` exactly once.
29. `attempt_count` counts actual retained attempt tasks.
30. `failure_count` counts every failed attempt.
31. `retry_attempt_count` counts every attempt after the first.
32. `retirement_retry_attempt_count` counts every retry attempt that begins at `RETIREMENT_SCHEDULING`.
33. Manager-level counters reconcile by monotonic per-job deltas, not one-time Boolean flags.
34. A second pending failure increments failure and retry counters by exactly one.
35. Re-observing the same outcome increments no counters.
36. Delayed successful completion increments recovery and delayed-completion counters exactly once.
37. Inline successful completion does not increment delayed-completion counters.
38. Counter updates remain correct when completion callback and request waiter race.

## Shutdown invariants

39. Shutdown stops new control requests first.
40. Shutdown waits for the active reload transaction before finalization drain.
41. The Boolean transaction-wait result is handled explicitly.
42. If transaction waiting succeeds, shutdown drains registered finalization jobs normally.
43. If transaction waiting times out, shutdown does not blindly proceed as though no transaction exists.
44. Timeout handling distinguishes:
   - preacceptance transaction still owning candidate/swap state;
   - accepted transaction with registered finalization owner;
   - accepted transaction in the narrow owner-registration recovery window.
45. Preacceptance timeout follows one bounded cancellation-and-cleanup path before runtime shutdown.
46. Accepted timeout ensures finalization ownership is registered or adopted before runtime shutdown.
47. Shutdown adoption is a distinct state from reference release.
48. `adopted_for_shutdown` is monotonic and independently observable.
49. `references_released` is monotonic and independently observable.
50. An adopted job can still release references after shutdown ownership transfer is recorded.
51. `FinalizationStatus.SHUTDOWN_ADOPTED` is emitted for unresolved work whose ownership moves to shutdown.
52. Runtime shutdown closes the exact unresolved old generation at most once.
53. Runtime shutdown closes the active generation exactly once.
54. A retirement task already scheduled before shutdown is not scheduled or closed a second time.

## Diagnostic invariants

55. The original reload result reflects finalization state at return time.
56. Manager snapshots reflect the latest reconciled state, including delayed completion after the original response.
57. Reload history or an associated finalization record reflects eventual completion without mutating immutable historical objects in place.
58. Completed history never reports stale active errors.
59. Shutdown-adopted records expose adoption explicitly and do not masquerade as completed finalization.
60. Terminal operational events distinguish:
   - accepted and completed;
   - accepted and retry-pending;
   - retirement scheduling failed;
   - shutdown adopted;
   - invariant failed.
61. Diagnostic persistence and transition facts derive from transaction state, not unconditional literals.

## Verification invariants

62. Tests claiming production `ReloadManager.reload()` coverage invoke the real integration harness.
63. Tests claiming transition-prefix rollback construct three actual transitions A, B, and C.
64. The A/B/C test proves:
   - A applies once;
   - B apply raises;
   - C apply never runs;
   - A rolls back once;
   - B and C rollback never run;
   - rollback ordering is reverse-prefix order;
   - SQLite changes are absent;
   - the staged swap is rolled back;
   - the old generation remains active;
   - the candidate closes once;
   - no finalization job is registered;
   - a subsequent reload succeeds.
65. Tests claiming deterministic database outcomes force exactly one outcome per test.
66. Tests claiming weak-reference collection assert each captured `weakref.ref()` becomes `None` after pruning and `gc.collect()`.
67. Tests claiming close-once instrument concrete generation-owned resources and assert exact integer counts.
68. Tests claiming shutdown adoption keep the finalization failure persistent through drain and execute the real lifespan shutdown ordering or an exact production helper.
69. Exact-head evidence names a full 40-character SHA and is generated after all code and test changes are complete.

---

# Workstream A — Complete the structural acceptance boundary

## A1. End the rollback-capable region before acceptance

Refactor the commit sequence into explicit phases:

```python
commit_context = await self._execute_preacceptance_commit(...)
accepted_context = self._mark_and_register_accepted(commit_context)
return await self._execute_accepted_phase(accepted_context, ...)
```

The exact names may differ, but the lexical structure is mandatory.

The preacceptance function or region may contain:

- SQLite transaction entry and exit;
- persistence mutation;
- staged runtime swap;
- process-transition application;
- SQLite rollback classification;
- runtime swap commit;
- preacceptance rollback and candidate cleanup.

It must return only after:

- SQLite commit succeeded;
- `pending_swap.commit()` succeeded;
- `txn.mark_runtime_swap_committed()` succeeded.

It must not call `txn.mark_accepted()`.

The accepted function or region must:

1. consume an immutable or tightly scoped commit context;
2. call `txn.mark_accepted()` outside rollback-capable handlers;
3. increment accepted/committed reload counters idempotently;
4. register the finalization owner synchronously;
5. attach process-owned completion reconciliation;
6. only then perform postacceptance awaits.

## A2. Introduce an accepted commit context

Use a typed record to eliminate reliance on scattered local variables after acceptance. Suggested fields:

```python
@dataclass(frozen=True)
class AcceptedCommitContext:
    transaction: ReloadTransaction
    candidate: RuntimeGenerationCandidate
    pending_swap: PendingGenerationSwap
    transition_result: TransitionApplyResult | None
    published_generation: RuntimeGeneration
    old_generation_id: int | None
    generation_id: int
    changed_sections: tuple[str, ...]
    started_at: float
    digest_prefix: str
```

This context should be fully constructed before leaving the preacceptance region.

## A3. Make acceptance accounting idempotent

Add a per-transaction or per-job acceptance-accounting marker. Suitable approaches:

- `txn.acceptance_accounted: bool` with a guarded transition method; or
- manager-owned `set[str]` of accounted request IDs bounded by finalization history.

Prefer transaction-local state because the transaction already owns acceptance facts.

Provide one method such as:

```python
def _record_reload_accepted_once(self, txn: ReloadTransaction) -> None:
    ...
```

It must update:

- `committed_reloads += 1`;
- `accepted_reloads += 1`;
- reload-level accepted metadata if present.

It must not update:

- `fully_finalized_reloads`;
- retry/failure counters;
- delayed-completion counters.

## A4. Add structural source guard

Add a focused AST/source test that fails if:

- `txn.mark_accepted()` occurs inside a `try` whose handler references `_abort_precommit_reload`; or
- the first postacceptance `await` appears before owner registration and completion reconciliation attachment.

This is supplementary to runtime tests.

### Acceptance criteria — Workstream A

- [ ] `txn.mark_accepted()` is outside rollback-capable handlers.
- [ ] Accepted context is fully constructed before acceptance.
- [ ] Accepted/committed counters increment before the first postacceptance await.
- [ ] Counter recording is idempotent.
- [ ] Owner registration and completion callback attachment occur before the first postacceptance await.
- [ ] Injected cancellation immediately after acceptance leaves accepted/committed counters incremented exactly once.
- [ ] Zero precommit cleanup functions run after acceptance.

---

# Workstream B — Make retirement scheduling a real progress state

## B1. Split observer completion from retirement attempt

After successful observer reporting:

```python
self._step = AcceptedFinalizationStep.RETIREMENT_SCHEDULING
```

Then dispatch `RETIREMENT_SCHEDULING` to the retirement attempt method.

The dispatch map should explicitly include:

```python
AcceptedFinalizationStep.OBSERVER_REPORTED: self._enter_retirement_scheduling
AcceptedFinalizationStep.RETIREMENT_SCHEDULING: self._attempt_retirement_scheduling
```

Alternatively, the observer step may advance directly to `RETIREMENT_SCHEDULING`, provided the actual `finalize_retirement()` call occurs in the next loop dispatch.

## B2. Preserve cursor on retirement failure

`pending_swap.finalize_retirement()` failure must leave:

- `_step == RETIREMENT_SCHEDULING`;
- health `RETRY_PENDING`;
- last failed step `retirement_scheduling`;
- status `RETIREMENT_SCHEDULE_FAILED`.

A later retry begins from `RETIREMENT_SCHEDULING` and increments:

- job `retry_attempt_count`;
- job `retirement_retry_attempt_count`;
- manager retry counters by delta.

## B3. Preserve exactly-once retirement

Retirement retry must continue to rely on `PendingGenerationSwap.finalize_retirement()` idempotence. Do not bypass the pending-swap owner or directly schedule a second retirement task.

Add assertions that:

- a one-shot failure followed by retry calls `finalize_retirement()` twice but schedules the old slot once;
- a timeout waiter plus later retry does not create duplicate retirement scheduling;
- a completed retirement step is never repeated.

### Acceptance criteria — Workstream B

- [ ] `RETIREMENT_SCHEDULING` is reached in production execution.
- [ ] Retirement failure leaves the cursor at `RETIREMENT_SCHEDULING`.
- [ ] Result category is `RETIREMENT_SCHEDULE_FAILED`.
- [ ] Snapshot and active-job diagnostics report the same status.
- [ ] Retirement retry counter increments only on actual retries from that cursor.
- [ ] Old-generation retirement is scheduled exactly once.

---

# Workstream C — Make reconciliation process-owned

## C1. Attach a retained-task completion callback

When a new retained attempt task is created, attach one callback that notifies the reload manager or a provided reconciliation owner.

Preferred design:

```python
class AcceptedReloadFinalizationJob:
    _on_attempt_done: Callable[[AcceptedReloadFinalizationJob, asyncio.Task[...]], None]
```

The callback must be non-blocking and schedule asynchronous reconciliation safely:

```python
def _attempt_done(task: asyncio.Task[AcceptedFinalizationOutcome]) -> None:
    loop.create_task(manager._observe_finalization_attempt(job, task))
```

Requirements:

- callback attachment occurs once per actual attempt task;
- callback does not retain the manager after job history pruning beyond the job lifecycle;
- callback handles successful outcome, failed outcome return, typed invariant exception, and cancellation;
- callback never raises into the event loop.

## C2. Centralize outcome observation

Introduce one process-owned method such as:

```python
async def _observe_finalization_attempt(
    self,
    job: AcceptedReloadFinalizationJob,
    task: asyncio.Task[AcceptedFinalizationOutcome],
) -> None:
    ...
```

It must:

1. extract or synthesize a structured outcome;
2. reconcile per-job accounting deltas;
3. if complete, move the job to history and release references;
4. if unresolved, keep the job active;
5. update canonical delayed diagnostics/events;
6. tolerate duplicate invocation.

Inline callers may still call the same observer directly after awaiting `job.run()`, but they must not own separate accounting logic.

## C3. Reconcile already-completed jobs before admission checks

Admission should not filter completed jobs out before reconciliation.

Before checking unresolved jobs, run a registry sweep:

```python
await self._reconcile_completed_registered_jobs()
```

Then construct the pending list from the remaining registry entries.

This is a defensive backstop for callback scheduling delays and interpreter/test seams.

## C4. Remove completed jobs from active registry

After complete reconciliation:

1. capture immutable history record;
2. update delayed diagnostic record;
3. remove from active registry;
4. release operational references;
5. clear callback/manager reference if one exists.

The order must prevent both lost history and retained live objects.

## C5. Handle postacceptance cancellation correctly

The cancellation handler must not create a second reconciliation contract.

For accepted transactions it should:

- ensure the owner exists;
- ensure process-owned completion reconciliation is attached;
- optionally await a bounded shielded prefix;
- if an outcome is obtained, pass it to the shared observer;
- if timeout/cancellation occurs, leave the retained task and callback running;
- finalize the cancelled request result without precommit cleanup;
- re-raise cancellation if that remains the public contract.

It must not leave a completed job unreconciled.

### Acceptance criteria — Workstream C

- [ ] Finalization completion reconciles after all request waiters are cancelled.
- [ ] Completed jobs do not remain in the active registry.
- [ ] Operational references are released after callback-driven completion.
- [ ] Admission sweeps any completed-but-unreconciled jobs before pending checks.
- [ ] Reconciliation remains idempotent when callback and inline waiter race.
- [ ] A 100-iteration accepted-cancellation test leaves zero active jobs after tasks settle.

---

# Workstream D — Correct all accounting with monotonic deltas

## D1. Add explicit accounted cursors

Each job needs manager-accounted cursor values for:

- `accounted_attempt_count`;
- `accounted_failure_count`;
- `accounted_retry_attempt_count`;
- `accounted_retirement_retry_attempt_count`;
- `completion_accounted`;
- `recovery_accounted`;
- `delayed_completion_accounted`.

Prefer a dedicated manager-side dataclass or fields on the job rather than dynamic `object.__setattr__` names.

Suggested structure:

```python
@dataclass
class FinalizationAccountingCursor:
    attempts: int = 0
    failures: int = 0
    retries: int = 0
    retirement_retries: int = 0
    completion_accounted: bool = False
```

## D2. Reconcile every observation by delta

For each outcome:

```python
failure_delta = outcome.failure_count - cursor.failures
retry_delta = outcome.retry_attempt_count - cursor.retries
retirement_retry_delta = (
    outcome.retirement_retry_attempt_count - cursor.retirement_retries
)
```

Reject negative deltas as invariants.

Update counters by exact positive deltas, then advance the cursor.

Do not use `_failures_accounted: bool`.

## D3. Define delayed completion precisely

A finalization completion is delayed when any of these is true:

- completion occurred on attempt number greater than one;
- original reload response returned before completion;
- original reload waiter was cancelled or timed out after acceptance;
- shutdown drain or admission retry observed the completion.

Encode this as a job fact rather than inferring only from `attempt_count > 1`.

Suggested fields:

- `inline_response_returned_before_completion`;
- `completion_observation_path` enum;
- or `completed_inline: bool` captured by the reload manager.

## D4. Count accepted cancellation truthfully

An accepted request cancelled after acceptance must still increment:

- `committed_reloads` exactly once;
- `accepted_reloads` exactly once.

It may also increment:

- `cancellations` once for the request waiter;
- delayed-completion counters if finalization completes later.

The counters represent different facts and must not be mutually exclusive.

### Acceptance criteria — Workstream D

- [ ] First failure increments failures by one and retries by zero.
- [ ] Second failed attempt increments failures by one and retries by one.
- [ ] Successful third attempt increments retries by one and completion by one.
- [ ] Retirement retry increments only the retirement-specific counter.
- [ ] Duplicate observation increments nothing.
- [ ] Accepted cancellation increments acceptance and cancellation counters truthfully.
- [ ] Callback and waiter races do not double-count.

---

# Workstream E — Separate shutdown adoption from release

## E1. Introduce distinct lifecycle flags

Replace the overloaded `_released` state with independent fields:

```python
_adopted_for_shutdown: bool = False
_references_released: bool = False
```

Properties:

- `adopted_for_shutdown` returns only `_adopted_for_shutdown`;
- `references_released` returns only `_references_released`.

`adopt_for_shutdown()` must not set `references_released`.

`release_references()` must remain callable after adoption.

## E2. Define shutdown-adopted outcome

When shutdown takes ownership of unresolved work:

- mark `_adopted_for_shutdown = True`;
- record adoption timestamp and prior progress step;
- emit `FinalizationStatus.SHUTDOWN_ADOPTED` while unresolved;
- capture an immutable adoption record or terminal finalization-history record;
- remove the job from normal retry admission if the process is terminating;
- preserve enough ownership information for runtime shutdown to close the exact old slot;
- release references once runtime shutdown confirms ownership transfer or completion.

Do not mark the transaction `COMPLETED` merely because shutdown adopted ownership.

## E3. Centralize shutdown preparation

Move shutdown decision logic into a reload-manager method rather than directly iterating private job dictionaries from `app.py`.

Suggested API:

```python
@dataclass(frozen=True)
class ReloadShutdownPreparation:
    transaction_wait_completed: bool
    unresolved_jobs: int
    adopted_jobs: int
    active_transaction_state: str | None
    ownership_safe_for_runtime_shutdown: bool

async def prepare_for_shutdown(
    self,
    *,
    transaction_timeout_s: float,
    finalization_timeout_s: float,
) -> ReloadShutdownPreparation:
    ...
```

`app.py` should call this method and branch on its result before `runtime_manager.shutdown()`.

## E4. Handle transaction-wait timeout explicitly

When `wait_for_transaction_completion()` returns `False`:

### Preacceptance active transaction

- request bounded cancellation of the reload coroutine through a process-owned task handle or shutdown token;
- wait for `_abort_precommit_reload()` to restore swap, transitions, candidate, and gate;
- if cleanup cannot be confirmed, emit an invariant-level shutdown diagnostic and avoid closing dependencies still owned by the transaction until ownership is resolved.

### Accepted active transaction

- ensure accepted owner registration;
- attach process-owned reconciliation;
- drain or adopt the owner;
- only then permit runtime shutdown.

### Unknown/narrow window

- inspect transaction acceptance and pending-swap state;
- call one deterministic ownership recovery function;
- fail closed rather than assuming the active transaction is absent.

## E5. Prove exact close-once behavior

Instrument generation-owned resources with close counters keyed by generation ID.

Required assertions:

- old generation retained by unresolved committed swap closes exactly once;
- active generation closes exactly once;
- no generation closes twice when retirement was already scheduled;
- adopted job references are released after shutdown ownership transfer;
- no active job remains after shutdown preparation completes.

### Acceptance criteria — Workstream E

- [ ] Adoption and reference release are independent states.
- [ ] `SHUTDOWN_ADOPTED` is observable.
- [ ] `app.py` does not ignore transaction-wait timeout.
- [ ] Shutdown preparation returns a structured result.
- [ ] Runtime shutdown starts only when ownership is explicitly safe.
- [ ] Exact old and active generation close counts are asserted.
- [ ] Adopted jobs release references.

---

# Workstream F — Reconcile canonical diagnostics after delayed outcomes

## F1. Preserve immutable original response

Do not mutate the `ReloadResult` already returned to the caller.

It should continue to represent finalization state at response time.

## F2. Update manager-visible canonical state

When a delayed attempt changes finalization status, update manager-visible state using a new immutable diagnostic record.

Possible approach:

- maintain `dict[request_id, ReloadDiagnosticResult]` for bounded current diagnostics;
- replace the matching record in `_reload_history` with `dataclasses.replace(...)`; or
- append a separate immutable finalization lifecycle event linked by request ID.

Whichever approach is selected, snapshots must report the latest finalization state for the request.

## F3. Emit delayed lifecycle events

Emit best-effort events for:

- `reload_finalization_retry_failed`;
- `reload_finalization_completed_delayed`;
- `reload_retirement_schedule_failed`;
- `reload_finalization_shutdown_adopted`;
- `reload_finalization_invariant_failed`.

Events must contain bounded, secret-free fields:

- request ID;
- generation ID;
- old generation ID;
- status;
- next step;
- attempt/failure/retry counters;
- error class;
- sanitized bounded error message.

## F4. Keep history truthful

Completed finalization history must contain:

- final status;
- attempts;
- failures;
- retries;
- retirement retries;
- last historical failure, if intentionally retained;
- no active error fields;
- completion/adoption timestamp;
- completion observation path.

### Acceptance criteria — Workstream F

- [ ] Original response remains immutable.
- [ ] Snapshot reflects delayed completion.
- [ ] Reload history or linked lifecycle history reflects delayed completion.
- [ ] Retirement failure status is consistent across all surfaces.
- [ ] Shutdown adoption is visible and not labeled completed.
- [ ] Events are bounded and secret-free.

---

# Workstream G — Replace verification substitutes with production proofs

## G1. Real A/B/C transition-prefix rollback

Create three concrete test transitions and inject them through the production transition-plan construction seam used by `ReloadManager.reload()`.

Example transitions:

```python
A.apply() -> append("A.apply")
A.rollback() -> append("A.rollback")
B.apply() -> append("B.apply"); raise RuntimeError("B failed")
B.rollback() -> append("B.rollback")
C.apply() -> append("C.apply")
C.rollback() -> append("C.rollback")
```

Expected trace:

```text
A.apply
B.apply
A.rollback
```

Forbidden trace entries:

```text
C.apply
B.rollback
C.rollback
```

The test must also assert the full database, swap, candidate, registry, and subsequent-reload invariants.

Do not use `TEST_INJECT_TRANSITION_APPLY_FAILURE` before `apply_all()` for this proof.

## G2. Deterministic database outcome matrix

Add explicit test seams at the `Database.transaction()` commit/rollback boundary to force:

1. commit raises, rollback succeeds, `in_transaction` becomes false:
   - outcome exactly `rolled_back`;
   - connection remains reusable;
   - invalidation false.
2. commit raises, rollback raises:
   - outcome exactly `indeterminate`;
   - connection invalidated;
   - subsequent operation raises `DatabaseConnectionInvalidatedError`.
3. commit raises and driver reports no active transaction before a confirmable rollback:
   - documented exact outcome;
   - test asserts the selected policy without `in (...)` alternatives.
4. nested transaction behavior remains unchanged.

Remove conditional assertions such as:

```python
if err.outcome == "indeterminate":
    ...
```

Each test must force and assert one branch.

## G3. Real weak-reference collection

Capture weak references while a job is intentionally unresolved:

- transaction;
- candidate;
- pending swap;
- transition result;
- published generation;
- observer/app target where weak-referenceable;
- job itself after registry removal.

Then:

1. allow retry or callback completion;
2. reconcile and prune;
3. drop local strong references;
4. run `gc.collect()` repeatedly within a bounded loop;
5. assert each expected `weakref.ref()` is `None`.

Exclude objects intentionally retained by the active runtime generation and document those exclusions.

## G4. Accepted-cancellation retention stress

Run at least 100 alternating reload attempts with cancellation injected after acceptance.

For every iteration:

- accepted generation remains authoritative;
- no rollback or candidate abort occurs;
- retained task completes after seam release;
- callback reconciles completion;
- active registry returns to zero;
- history remains bounded;
- accepted and finalized counters advance exactly once per accepted reload;
- weak references from sampled iterations become collectible.

## G5. Real shutdown adoption test

Use a persistent retirement failure seam that remains active through:

- inline finalization;
- explicit drain;
- shutdown preparation.

Then execute the production shutdown preparation and runtime shutdown path.

Assert:

- transaction wait outcome;
- drain remains unresolved;
- job becomes `SHUTDOWN_ADOPTED`;
- old-generation resource closes exactly once;
- active-generation resource closes exactly once;
- job references release;
- active registry empties or moves into scalar-only shutdown history;
- no duplicate retirement task is created.

## G6. Transaction-wait timeout test

Create an actual reload blocked before acceptance with a deterministic barrier.

Call shutdown preparation with a short transaction timeout.

Assert the exact timeout branch:

- shutdown does not immediately call runtime shutdown;
- the reload receives cancellation/shutdown signal;
- preacceptance cleanup completes;
- lease admission reopens;
- candidate closes once;
- old generation remains active until runtime shutdown;
- runtime shutdown then closes the active generation once.

Add a separate accepted blocked-finalization timeout test.

## G7. Structural source test

Use Python AST inspection rather than brittle substring matching to assert:

- `mark_accepted()` is not inside a rollback-capable `try`;
- owner registration precedes the first postacceptance await;
- accepted counter recording precedes the first postacceptance await.

### Acceptance criteria — Workstream G

- [ ] A/B/C production trace is exactly correct.
- [ ] Database tests have no alternate accepted outcomes.
- [ ] Weak-reference tests assert references become `None`.
- [ ] Accepted-cancellation stress leaves no active jobs.
- [ ] Shutdown adoption keeps failure persistent through drain.
- [ ] Exact per-generation close counts are asserted.
- [ ] Transaction timeout branches are exercised through production helpers.
- [ ] AST structural guard passes.

---

# Workstream H — CI and exact-head closure evidence

## H1. Focused Plan 021 CI job

Extend the existing Python 3.11/3.12 reload-closure matrix with all Plan 021 tests.

Required focused files should include, at minimum:

```text
tests/unit/test_accepted_finalization_state_machine.py
tests/unit/test_reload_diagnostics_matrix.py
tests/integration/reload/test_plan_021_acceptance_accounting.py
tests/integration/reload/test_plan_021_retirement_progress.py
tests/integration/reload/test_plan_021_process_owned_reconciliation.py
tests/integration/reload/test_plan_021_shutdown_adoption.py
tests/integration/reload/test_plan_021_transition_prefix_production.py
tests/integration/reload/test_plan_021_database_outcomes.py
tests/integration/reload/test_plan_021_retention_close_counts.py
```

Use actual final filenames if consolidated differently.

## H2. Repeated lifecycle execution

Run the focused lifecycle suite at least three consecutive times on the exact implementation head:

```bash
for i in 1 2 3; do
  uv run pytest \
    tests/unit/test_accepted_finalization_state_machine.py \
    tests/integration/reload/test_plan_019_*.py \
    tests/integration/reload/test_plan_020_*.py \
    tests/integration/reload/test_plan_021_*.py \
    -q --tb=short || exit 1
done
```

The shell pattern may be replaced with explicit file lists in CI for portability.

## H3. Full verification commands

Run at the final implementation SHA:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run python scripts/audit_xfail_skips.py
uv run pytest tests/integration/reload/ -q --tb=short
uv run pytest tests/ -m "not slow and not performance and not soak and not extended_soak and not live" -q --tb=short
```

Also run any project-standard security or dependency audit already required by CI.

## H4. Exact-head artifact

Create or replace:

```text
artifacts/plan-021-evidence.md
```

It must contain:

- exact full 40-character implementation SHA;
- UTC timestamp;
- Python versions;
- exact commands;
- pass/fail counts;
- three repeated focused-run results;
- full reload suite result;
- full standard suite result;
- lint/format/typecheck/audit results;
- links or identifiers for repository-visible CI runs when available;
- explicit statement that no code or test changes occurred after the evidenced SHA.

Do not write `HEAD`, abbreviated SHA, or “pre-existing failure” as closure evidence.

If a failure is discovered, fix it and regenerate all evidence at the new head.

## H5. Plan status discipline

Keep this plan status as `implementation handoff` until all closure gates pass.

Only change to `completed` in the same commit or a later documentation-only commit when:

- evidence names the exact implementation SHA;
- no code or test changes occurred afterward;
- CI status is green or archived machine output is attached and exact-head reproducible;
- every global closure gate is checked.

### Acceptance criteria — Workstream H

- [ ] Python 3.11 and 3.12 focused jobs pass.
- [ ] Three consecutive focused runs pass.
- [ ] Full reload-control suite passes.
- [ ] Full standard non-soak/non-performance suite passes.
- [ ] Ruff format/check pass.
- [ ] Pyright passes.
- [ ] Skip/xfail audit passes.
- [ ] Evidence contains the full final SHA.
- [ ] No code/test commit follows the evidenced SHA.

---

# Recommended implementation order

## Milestone 1 — Lifecycle facts and progress

Implement:

- Workstream A acceptance extraction;
- acceptance accounting;
- Workstream B retirement progress transition;
- unit tests for exact state/status/counter semantics.

Exit criteria:

- structural AST test passes;
- retirement failure returns `RETIREMENT_SCHEDULE_FAILED`;
- accepted counters are correct under immediate cancellation.

## Milestone 2 — Process-owned reconciliation

Implement:

- completion callback/observer;
- registry sweep;
- delta accounting cursors;
- callback/waiter race handling;
- delayed diagnostic updates.

Exit criteria:

- completed jobs cannot remain active;
- repeated cancellation stress passes;
- counters reconcile exactly.

## Milestone 3 — Shutdown ownership closure

Implement:

- distinct adoption/release state;
- manager-level shutdown preparation;
- explicit transaction-timeout branches;
- shutdown diagnostics and close-once instrumentation.

Exit criteria:

- persistent-failure adoption test passes;
- preacceptance timeout test passes;
- exact old/active close counts pass.

## Milestone 4 — Production proof and evidence

Implement:

- A/B/C production rollback test;
- deterministic database matrix;
- real weak-reference assertions;
- CI matrix updates;
- exact-head evidence.

Exit criteria:

- all global closure gates pass;
- evidence artifact is tied to the final implementation SHA.

---

# Expected code touch points

Likely production files:

```text
src/eggpool/app.py
src/eggpool/control/accepted_finalization.py
src/eggpool/control/reload_manager.py
src/eggpool/reload_diagnostics.py
src/eggpool/reload_transaction.py
src/eggpool/runtime_manager.py
src/eggpool/db/core.py                 # or current Database implementation path
src/eggpool/errors.py
.github/workflows/ci.yml
```

Likely test files:

```text
tests/unit/test_accepted_finalization_state_machine.py
tests/unit/test_reload_diagnostics_matrix.py
tests/integration/reload/test_plan_021_acceptance_accounting.py
tests/integration/reload/test_plan_021_retirement_progress.py
tests/integration/reload/test_plan_021_process_owned_reconciliation.py
tests/integration/reload/test_plan_021_shutdown_adoption.py
tests/integration/reload/test_plan_021_transition_prefix_production.py
tests/integration/reload/test_plan_021_database_outcomes.py
tests/integration/reload/test_plan_021_retention_close_counts.py
```

Documentation/evidence:

```text
artifacts/plan-021-evidence.md
AGENTS.md                          # only if CI command registry requires update
.opencode/skills/architecture/SKILL.md  # only if project convention requires plan registry update
```

Avoid touching unrelated provider, dispatch, transcoding, dashboard, pricing, model-catalog, or request-routing files.

---

# Required test scenarios

## Scenario 1 — Accepted cancellation before inline finalization returns

Given:

- SQLite committed;
- runtime swap committed;
- transaction marked accepted;
- owner registered;
- retained task blocked in a finalization step.

When:

- request waiter is cancelled.

Then:

- accepted/committed counters are already incremented once;
- cancellation counter increments once;
- retained task remains running;
- no rollback or candidate abort occurs;
- task completion callback reconciles finalization;
- finalized counter increments once;
- active registry empties;
- references release.

## Scenario 2 — Two retirement failures then success

Attempt 1:

- enters `RETIREMENT_SCHEDULING`;
- retirement fails;
- failures=1, retries=0, retirement_retries=0.

Attempt 2:

- starts at `RETIREMENT_SCHEDULING`;
- retirement fails;
- failures=2, retries=1, retirement_retries=1.

Attempt 3:

- starts at `RETIREMENT_SCHEDULING`;
- retirement succeeds;
- failures=2, retries=2, retirement_retries=2;
- completion counted once;
- recovery counted once;
- old generation scheduled once.

## Scenario 3 — Callback and waiter race

Given two callers awaiting one retained attempt:

- attempt completes;
- task callback schedules reconciliation;
- both waiters receive the same outcome and invoke shared observation.

Then all global counters change exactly once and the job is pruned once.

## Scenario 4 — Shutdown timeout before acceptance

Given a reload blocked before SQLite commit or runtime swap commit:

- transaction wait times out.

Then shutdown requests bounded cancellation, precommit cleanup completes, candidate closes once, gate reopens, and runtime shutdown starts only after ownership is safe.

## Scenario 5 — Shutdown timeout after acceptance

Given an accepted reload blocked in persistent retirement failure:

- transaction wait/drain cannot fully resolve.

Then owner is registered, status becomes `SHUTDOWN_ADOPTED`, runtime shutdown closes old and active generations exactly once, and job references release.

## Scenario 6 — Real transition prefix rollback

Trace must be exactly:

```text
A.apply
B.apply
A.rollback
```

No other transition method runs.

## Scenario 7 — Database rolled-back branch

Commit raises, rollback succeeds, connection remains reusable, outcome is exactly `rolled_back`.

## Scenario 8 — Database indeterminate branch

Commit raises, rollback fails or cannot confirm state, connection becomes invalidated, and subsequent use raises the typed invalidation error.

## Scenario 9 — Weak-reference release

Capture weak references from an unresolved job, complete and reconcile it, remove all local strong references, force collection, and assert every expected weak reference is `None`.

---

# Global closure gate

This line of work is closed only when every statement below is true and evidenced:

1. [ ] `txn.mark_accepted()` is outside rollback-capable handlers.
2. [ ] Acceptance ownership is registered before the first postacceptance await.
3. [ ] Accepted/committed counters increment at acceptance exactly once.
4. [ ] `RETIREMENT_SCHEDULING` is a real production cursor state.
5. [ ] Retirement failure reports `RETIREMENT_SCHEDULE_FAILED`.
6. [ ] Retirement retry counters advance correctly.
7. [ ] Retained attempts have process-owned completion reconciliation.
8. [ ] Cancelling all waiters cannot suppress reconciliation.
9. [ ] Completed jobs cannot remain in the active registry.
10. [ ] Active registry contains unresolved jobs only.
11. [ ] Failure/retry accounting uses monotonic deltas.
12. [ ] A second failed retry is counted.
13. [ ] Callback/waiter races do not double-count.
14. [ ] Accepted cancellation counts acceptance and cancellation truthfully.
15. [ ] Shutdown adoption is distinct from reference release.
16. [ ] `SHUTDOWN_ADOPTED` is observable.
17. [ ] Transaction-wait timeout is handled explicitly.
18. [ ] Runtime shutdown begins only after ownership is safe.
19. [ ] Exact old-generation close count is one.
20. [ ] Exact active-generation close count is one.
21. [ ] Delayed completion updates manager-visible diagnostics.
22. [ ] A/B/C transition-prefix rollback runs through `ReloadManager.reload()`.
23. [ ] Database rolled-back and indeterminate branches are deterministic.
24. [ ] Weak-reference tests assert collection.
25. [ ] 100 accepted-cancellation iterations leave zero active jobs.
26. [ ] Finalization history remains bounded and scalar-only.
27. [ ] Plan 019 and Plan 020 focused suites remain green.
28. [ ] Python 3.11 and 3.12 focused CI pass.
29. [ ] Three consecutive focused lifecycle runs pass.
30. [ ] Full reload-control suite passes.
31. [ ] Full standard non-soak/non-performance suite passes.
32. [ ] Ruff format/check, pyright, and skip/xfail audit pass.
33. [ ] Evidence identifies the exact full final implementation SHA.
34. [ ] No code or test commit follows the evidenced SHA.

Until all 34 statements are evidenced, Plan 021 remains open and Plans 019–020 should not be treated as terminally verified.

---

# Handoff notes

The implementation agent should begin by reproducing the current defects on baseline `e3bd47c0cedb55449b7f73a87698b359adbd678a`:

- inspect the lexical placement of `txn.mark_accepted()`;
- inject one and two retirement failures and observe the unreachable retirement-specific status/counter;
- cancel an accepted reload waiter, allow the retained task to finish, and inspect the active registry and counters;
- invoke shutdown preparation with an active blocked transaction and confirm the wait result is currently ignored;
- run the current A/B/C, database, weak-reference, and close-once tests and document which claimed assertions are absent.

Preserve the working Plan 020 retained-task design. Do not revert to lock-only serialization or waiter-owned finalization. The corrective direction is to complete the process-owned lifecycle around that retained task, not replace it.
