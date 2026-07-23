# Accepted-Finalization Lifecycle Closure

Date: 2026-07-23
Status: implementation handoff

Depends on:

- `plans/015-reload-atomicity-final-closure.md`
- `plans/016-reload-atomicity-corrective-closure.md`
- `plans/017-reload-atomicity-final-corrective-pass.md`
- `plans/018-reload-atomicity-closure-corrective-pass.md`

Implementation baseline:

- `ecb4951e3434b97b3a287db62a9fc623d880d50f`

## Objective

Close the remaining correctness and lifecycle defects introduced or left open by the Plan 018 accepted-finalization implementation.

Plan 018 materially improved reload safety:

- the reload owner now retains `TransitionApplyResult` before transition application;
- accepted transactions reject abort transitions;
- committed pending swaps are no longer force-cleared;
- indeterminate SQLite connections are detached and invalidated;
- lease-gate repair no longer mutates the publication epoch;
- a process-owned accepted-finalization job exists.

Those improvements must be retained.

This pass is required because the current accepted-finalization implementation still permits invalid completion, retry, shutdown, and resource-ownership states:

1. `AcceptedFinalizationStep.DEGRADED` is treated as complete.
2. Calling `run()` again from `DEGRADED` skips all unfinished steps and then marks the job `COMPLETED`.
3. Completed jobs remain in `_accepted_finalization_jobs` and retain the transaction, candidate, pending swap, transition result, old and new generation graphs, app, and observer indefinitely.
4. `drain_finalization_jobs()` exists but is not invoked by application shutdown.
5. `TransitionApplyResult.finalize_all()` returns remaining failures, but the production finalization job ignores the outcome and advances as though finalization completed.
6. A degraded accepted reload is returned as an ordinary completed success without truthful finalization status.
7. `TEST_INJECT_RETIREMENT_FAILURE` is declared and used by tests but is not connected to the production retirement step.
8. Retirement tests do not prove that the exact original old generation is eventually scheduled and closed.
9. The rollback-capable inner `try` still extends beyond `txn.mark_accepted()` through finalization-job construction and registration.
10. Exception-specific outer handlers can still call abort transitions without first checking `txn.reload_accepted`.
11. The indeterminate-commit test permits either branch and therefore does not deterministically prove connection invalidation.
12. Mock candidate ownership fallback still compares uppercase strings instead of canonical lowercase enum values.
13. No exact-head CI evidence demonstrates the corrected lifecycle under repeated reloads and shutdown.

This plan is deliberately narrow. It does not redesign live reload. It makes the existing Plan 018 architecture truthful, retryable, bounded, and safe for long-running processes.

---

# Scope

## In scope

- accepted-finalization job state semantics;
- retry from the exact failed finalization step;
- single-flight execution of a finalization job;
- transition-finalization outcome handling;
- retirement-failure injection at the real production boundary;
- exact old-generation retirement ownership and close-count proof;
- active finalization registry pruning;
- release of strong operational references after completion;
- bounded lightweight finalization history;
- shutdown ordering and unresolved-finalization adoption;
- structural separation of accepted control flow from rollback-capable handlers;
- truthful accepted/pending/degraded/completed diagnostics and counters;
- deterministic indeterminate SQLite commit tests;
- canonical candidate ownership comparisons;
- focused Python 3.11/3.12 CI and exact-head evidence.

## Explicit non-goals

Do not:

- redesign config validation or semantic diffing;
- change which config fields are live, ignored, or restart-required;
- replace `RuntimeManager`, generation leases, or pending swaps;
- replace SQLite or aiosqlite;
- add distributed or crash-recoverable reload transactions;
- change provider routing, request dispatch, transcoding, accounting, or dashboard behavior unrelated to reload diagnostics;
- make observer delivery a prerequisite for accepting a generation;
- introduce an unbounded generic background retry framework;
- retain completed finalization jobs solely for diagnostics;
- allow a later reload to bypass unresolved accepted finalization;
- treat a failed finalization attempt as completed merely because the active generation is already authoritative;
- broaden this pass into unrelated Plan 015–018 cleanup.

---

# Required lifecycle model

## Finalization progress and health are separate concepts

Do not encode both progress and failure health in one enum value.

Use one monotonic progress cursor and one independent attempt outcome.

A suitable model is:

```python
class AcceptedFinalizationStep(enum.Enum):
    OWNERSHIP_TRANSFER = "ownership_transfer"
    MIRROR_UPDATE = "mirror_update"
    TRANSITIONS_FINALIZATION = "transitions_finalization"
    OBSERVER_REPORT = "observer_report"
    RETIREMENT_SCHEDULING = "retirement_scheduling"
    TRANSACTION_COMPLETION = "transaction_completion"
    COMPLETED = "completed"

class AcceptedFinalizationHealth(enum.Enum):
    READY = "ready"
    RUNNING = "running"
    RETRY_PENDING = "retry_pending"
    COMPLETED = "completed"
```

Equivalent names are acceptable, but the semantics are mandatory:

- the progress cursor identifies the next required step;
- an exception does not advance the progress cursor;
- health records whether the latest attempt failed;
- only the `COMPLETED` progress state is complete;
- there is no terminal degraded state for retryable operational work;
- diagnostics may report degraded or retry-pending health without claiming completion.

## Required invariants

### Completion and retry invariants

1. `job.is_complete` is true only when every required finalization step completed.
2. A failed attempt leaves the progress cursor at the failed step.
3. A second `run()` retries the failed step before any later step.
4. Completed steps are not repeated.
5. A failed step cannot be skipped because of an error-state enum value.
6. `run()` never unconditionally assigns `COMPLETED` after step guards return.
7. `run()` returns a structured outcome containing:
   - completion status;
   - next pending step;
   - attempt count;
   - failed step;
   - error class and message;
   - whether retry is permitted.
8. Concurrent calls to `run()` execute one finalization attempt, not two overlapping attempts.
9. Cancellation of a waiter does not cancel or erase the process-owned job.
10. New reload admission treats every non-complete job as unresolved.
11. Shutdown draining treats every non-complete job as unresolved.

### Transition-finalization invariants

12. `TransitionApplyResult.finalize_all()` remains the authoritative transition-finalization primitive.
13. If its returned outcome contains `remaining`, the accepted-finalization job does not advance past `TRANSITIONS_FINALIZATION`.
14. A retry invokes only remaining transitions.
15. Transaction facts such as `transitions_finalized=True` are set only when `remaining` is empty.
16. Transition-finalization failures remain visible in job and reload diagnostics.
17. Observer reporting and retirement scheduling cannot run before transition finalization is complete.

### Retirement invariants

18. Retirement scheduling failure leaves the job pending at `RETIREMENT_SCHEDULING`.
19. The committed pending swap retains the exact original old slot until scheduling succeeds or shutdown explicitly adopts it.
20. A later retry schedules the exact old generation retained by that swap.
21. The old generation is registered for retirement exactly once.
22. Generation-owned resources close exactly once.
23. A new swap is not prepared while a committed prior swap remains unresolved.
24. Retirement fault injection executes inside the production retirement step immediately before the real scheduling call.
25. Removing the injected failure allows the same job to resume and complete.

### Registry and reference-lifetime invariants

26. The active finalization registry contains unresolved jobs only.
27. A completed job is removed from the active registry synchronously with completion publication.
28. Completed job information is copied into a small immutable diagnostic record.
29. Diagnostic history is bounded, with an explicit maximum such as 32 or 64 records.
30. After completion, no retained diagnostic record holds references to:
   - candidate containers;
   - pending swaps;
   - transition objects or results;
   - runtime generations or slots;
   - app objects;
   - observers;
   - reload managers;
   - database or process objects.
31. The job clears or drops all operational references after successful completion and registry removal.
32. Repeated successful reloads do not cause the active registry or retained generation graph count to grow.
33. Snapshot generation does not expose live job objects directly.

### Acceptance-boundary invariants

34. Once `txn.reload_accepted` is true, no exception handler may call:
   - `_abort_precommit_reload()`;
   - `pending_swap.rollback()`;
   - `transition_result.rollback_applied()`;
   - `candidate.abort()`;
   - `txn.mark_aborting()`;
   - `txn.mark_aborted()`.
35. Job allocation and all data needed for accepted ownership are prepared before the accepted boundary wherever possible.
36. After `txn.mark_accepted()`, control exits the rollback-capable block without executing user, observer, mirror, transition, retirement, logging, or event awaits.
37. Every exception-specific outer handler branches on `txn.reload_accepted` before any abort transition.
38. `ReloadPreparationError`, `DatabaseCommitError`, observer exceptions, and generic exceptions all obey the same accepted discriminator.
39. A defensive assertion prevents `_abort_precommit_reload()` from running for an accepted transaction.

### Shutdown invariants

40. Application shutdown stops new control requests first.
41. Shutdown waits for any currently executing reload transaction to leave its critical section.
42. Shutdown invokes `reload_manager.drain_finalization_jobs()` before `runtime_manager.shutdown()`.
43. Finalization drain is bounded and single-flight.
44. If retirement cannot be scheduled during bounded drain, the old slot is explicitly adopted by shutdown cleanup rather than orphaned.
45. Runtime shutdown closes both:
   - the active generation;
   - any old slot retained by a committed unresolved swap.
46. Shutdown close counts remain exactly once.
47. Database shutdown occurs only after finalization and runtime retirement handling.

### Diagnostic invariants

48. Accepted reload status distinguishes:
   - accepted and fully finalized;
   - accepted with retry-pending finalization;
   - accepted with retirement pending;
   - preacceptance failure;
   - cancellation before acceptance.
49. `ok` retains its documented API meaning, but it must not imply full finalization unless a separate `finalization_status` says `completed`.
50. A pending finalization result includes the exact next step and last error.
51. Completed reload counters do not count accepted-but-unfinalized work as fully finalized.
52. Add or rename counters so operators can distinguish:
   - accepted reloads;
   - fully finalized reloads;
   - accepted finalization retries;
   - accepted finalization failures;
   - unresolved jobs;
   - retirement scheduling retries.
53. Transaction facts are operation-derived, not inferred from the requested path.

---

# Workstream A — Correct the finalization job state machine

## A1. Remove `DEGRADED` as a completion state

`AcceptedFinalizationStep.DEGRADED` must not be considered complete.

Preferred correction:

- remove `DEGRADED` from the progress enum;
- retain the next required step unchanged on failure;
- store failure information in separate fields;
- define `is_complete` as `step is COMPLETED` only.

If `DEGRADED` remains for compatibility, it must be a health field and must not participate in progress guards or completion checks.

## A2. Make `run()` resume the failed step

Refactor `run()` into an explicit loop or dispatch table:

```python
while self.next_step is not COMPLETED:
    await self._run_current_step()
return completed_outcome
```

On step failure:

- record failure details;
- leave `next_step` unchanged;
- return `retry_pending`;
- do not execute later steps;
- do not mark the transaction complete.

Do not rely on a sequence of methods that silently return when the current enum is unexpected.

## A3. Add single-flight execution

Add an `asyncio.Lock` or retained task per job.

Required behavior:

- admission retry and shutdown drain may call the same job concurrently;
- only one attempt executes;
- other callers await the same execution result;
- waiter cancellation does not cancel the retained process-owned execution task;
- attempt counters increment once per actual execution, not once per waiter.

## A4. Clear stale error state after successful retry

When a previously failed step succeeds:

- clear or archive the prior error as attempt history;
- update `last_successful_step`;
- advance to the next step;
- retain a bounded attempt history if useful for diagnostics.

Do not lose the fact that retries occurred, but do not leave a completed job reporting an active error.

### Acceptance criteria — Workstream A

- [ ] `is_complete` is true only for `COMPLETED`.
- [ ] A failure leaves the next step unchanged.
- [ ] A retry executes the failed step.
- [ ] Later steps do not execute before the failed step succeeds.
- [ ] Two concurrent callers cause one execution.
- [ ] Cancellation of one waiter does not erase the job.
- [ ] No path can skip all step bodies and then assign `COMPLETED`.

---

# Workstream B — Honor transition-finalization outcomes

## B1. Inspect `TransitionFinalizeOutcome`

Change the transition-finalization step from unconditional advancement:

```python
await transition_result.finalize_all()
mark_transitions_finalized()
advance()
```

to outcome-driven advancement:

```python
outcome = await transition_result.finalize_all()
if outcome.remaining:
    raise TransitionFinalizationPending(outcome)
mark_transitions_finalized()
advance()
```

A dedicated typed exception or structured non-exception result is acceptable.

## B2. Preserve per-transition retry information

Job diagnostics must include:

- attempted transition names;
- newly finalized transition names;
- remaining transition names;
- failure classes and messages.

Do not retain exception tracebacks or transition objects in completed history.

## B3. Add a fail-once production test

Use two applied transitions:

- A finalizes successfully;
- B fails on its first finalize call and succeeds on its second.

Prove:

1. first job run stops at transition finalization;
2. observer and retirement are not invoked;
3. job is unresolved;
4. retry calls only B;
5. job then advances through observer and retirement;
6. transaction becomes `COMPLETED` only after B succeeds.

### Acceptance criteria — Workstream B

- [ ] Production checks `remaining` from `TransitionFinalizeOutcome`.
- [ ] `transitions_finalized` remains false while any transition remains.
- [ ] Retry invokes only remaining transitions.
- [ ] Observer and retirement wait for successful transition finalization.
- [ ] The fail-once production test passes.

---

# Workstream C — Bound the registry and release object graphs

## C1. Separate active jobs from diagnostic history

Replace the unbounded list of live jobs with:

```python
_active_finalization_jobs: dict[str, AcceptedReloadFinalizationJob]
_finalization_history: deque[AcceptedFinalizationRecord]
```

Use request ID or generation ID as a stable key.

The active map contains unresolved jobs only.

## C2. Define a lightweight immutable record

The completed history record may contain only scalar or immutable diagnostic data:

- request ID;
- generation IDs;
- completion status;
- attempts;
- retry count;
- timestamps and durations;
- prior failed steps;
- final error summary if shutdown degraded;
- retirement task generation ID.

It must not contain live runtime objects.

## C3. Release operational references

After successful completion and before dropping the active job:

- create the immutable record;
- remove the job from the active registry;
- clear candidate reference;
- clear pending-swap reference;
- clear transition-result reference;
- clear published-generation reference;
- clear app reference;
- clear observer reference;
- clear reload-manager reference;
- clear transaction reference if the history record already contains the required facts.

Use explicit `release_references()` semantics rather than depending on garbage collection timing.

## C4. Bound history

Set and document a fixed maximum.

Recommended:

```python
FINALIZATION_HISTORY_MAX = 32
```

Snapshot output may include active jobs and bounded history separately.

## C5. Add a long-running retention test

Run at least 100 successful alternating reloads using resources that expose weak references or explicit close trackers.

After forcing garbage collection, prove:

- active finalization job count is zero;
- finalization history length is at most the configured maximum;
- all superseded candidate, swap, transaction, and generation test objects are collectible;
- only the active generation and genuinely running retirement tasks remain strongly owned;
- completed old resources close exactly once.

Do not make this a timing-only assertion. Use weak references and deterministic task draining.

### Acceptance criteria — Workstream C

- [ ] Active registry contains unresolved jobs only.
- [ ] Completed jobs are removed immediately.
- [ ] Completed history is bounded.
- [ ] History contains no operational references.
- [ ] Job references are explicitly released.
- [ ] The 100-reload retention test proves no monotonic retained-job growth.

---

# Workstream D — Make retirement failure real and retryable

## D1. Wire the fault seam at the real boundary

Either remove `TEST_INJECT_RETIREMENT_FAILURE` or invoke it in `_step_retirement_scheduling()` immediately before:

```python
await pending_swap.finalize_retirement()
```

The seam must be:

- test-only;
- instance-scoped;
- one-shot unless the test explicitly re-arms it;
- triggered only after acceptance and before real retirement scheduling.

Prefer a generic exact-step fault hook over accumulating individual attributes, for example:

```python
TEST_INJECT_FINALIZATION_FAILURES: dict[AcceptedFinalizationStep, BaseException]
```

## D2. Preserve the committed swap on failure

When retirement scheduling fails:

- job remains active at `RETIREMENT_SCHEDULING`;
- pending swap remains `COMMITTED`;
- old slot remains strongly owned by the pending swap;
- runtime manager refuses another pending swap;
- lease admission for the active generation remains open;
- diagnostics report the exact old generation ID.

## D3. Retry before admitting another reload

Admission behavior:

1. find unresolved active finalization jobs;
2. run bounded single-flight retry;
3. if the retirement step completes, remove the completed job and proceed;
4. if it remains unresolved, reject the new reload with a typed busy/finalization-pending error.

A failed job must never be filtered out as complete.

## D4. Prove exact retirement ownership

The production-path test must track generation-specific resources.

Required sequence:

1. generation 1 becomes active;
2. generation 2 is accepted;
3. retirement scheduling for generation 1 fails once;
4. active generation is 2 and accepts leases;
5. pending swap remains committed and reports old generation 1;
6. generation 1 resources remain open before retry;
7. retry schedules generation 1, not generation 0 or generation 2;
8. generation 1 resources close exactly once;
9. pending swap clears only after scheduling succeeds;
10. generation 3 reload is admitted only after this resolution.

### Acceptance criteria — Workstream D

- [ ] Retirement injection executes in production code.
- [ ] First failure leaves a retryable job and committed swap.
- [ ] New reload is blocked until retry resolves the job.
- [ ] Retry schedules the exact original old generation.
- [ ] Old resources close exactly once.
- [ ] Existing retirement tests are replaced if they only prove a later reload succeeds.

---

# Workstream E — Wire shutdown finalization ownership

## E1. Correct application shutdown ordering

In application lifespan shutdown, use this order:

1. stop the control server;
2. obtain `reload_manager`;
3. wait for an active reload critical section with a bounded timeout;
4. call `reload_manager.drain_finalization_jobs()`;
5. resolve or adopt any committed unresolved swap;
6. call `runtime_manager.shutdown()`;
7. stop remaining process-owned writers/probes;
8. close databases.

Do not call `runtime_manager.shutdown()` before accepted-finalization draining.

## E2. Handle unresolved drain deterministically

A timeout or repeated retirement failure must not orphan the old slot.

Implement one explicit shutdown-only ownership transfer, for example:

```python
await finalization_job.adopt_for_shutdown(runtime_manager)
```

or:

```python
await pending_swap.finalize_or_adopt_retirement_for_shutdown()
```

Required behavior:

- runtime manager becomes the owner of the old slot;
- the old slot is entered into shutdown retirement exactly once;
- the pending swap is moved to a truthful terminal shutdown-adopted state or cleared after adoption;
- the job emits a lightweight shutdown-degraded record;
- no object graph is left solely in an abandoned job.

Do not simply discard the job because the process is exiting; tests must be able to prove resource closure.

## E3. Drain active execution safely

If a job is already running:

- shutdown awaits the same retained execution task;
- it does not start a duplicate attempt;
- timeout handling does not cancel a step after ownership transfer without retaining the job;
- shutdown adoption occurs only after the running attempt has reached a stable boundary.

## E4. Add shutdown integration tests

Test both:

### Successful drain

- inject a fail-once retirement failure;
- clear the seam;
- begin shutdown;
- drain retries and schedules the old generation;
- runtime shutdown closes old and active generations exactly once;
- database closes afterward.

### Persistent failure

- retirement scheduling continues to fail;
- bounded drain expires;
- shutdown adopts the old slot;
- old and active resources close exactly once;
- no active finalization job retains runtime objects after shutdown.

### Acceptance criteria — Workstream E

- [ ] Lifespan calls finalization drain before runtime shutdown.
- [ ] Active reload completion is awaited before drain.
- [ ] Running jobs are single-flight during shutdown.
- [ ] Persistent failure transfers old-slot ownership to shutdown cleanup.
- [ ] Shutdown tests prove exact close counts and ordering.
- [ ] Database shutdown occurs after finalization/runtime cleanup.

---

# Workstream F — Finish the acceptance boundary

## F1. Remove accepted work from the aborting inner `try`

The block whose handlers call `_abort_precommit_reload()` must not contain post-acceptance work.

Prepare the job object and its immutable metadata before the irreversible handoff where feasible.

After runtime swap commit, perform only a synchronous handoff sequence with no awaits:

1. establish finalization ownership in the active registry;
2. mark the transaction accepted;
3. exit the rollback-capable block.

Then invoke the finalization executor in a separate accepted-only block.

If the implementation keeps registration after `mark_accepted()`, every handler surrounding it must branch on `txn.reload_accepted` before cleanup. The preferred solution is to make the ownership handoff one small helper with no external callbacks or awaits.

## F2. Add a defensive accepted guard to cleanup

At the beginning of `_abort_precommit_reload()`:

```python
if txn.reload_accepted:
    raise TransactionStateError(
        "accepted reload cannot enter precommit cleanup"
    )
```

This is a correctness assertion, not ordinary recovery behavior.

## F3. Guard every exception-specific handler

Before `ReloadPreparationError`, cancellation, generic exception, or other typed handlers call any transaction abort method, they must check `txn.reload_accepted`.

Consolidate accepted exception handling where practical:

```python
if txn.reload_accepted:
    return await self._handle_accepted_failure(...)
```

No exception class may bypass this discriminator.

## F4. Add exact boundary tests

Use barriers or exact fault hooks at these locations:

- immediately before acceptance;
- immediately after finalization owner registration;
- immediately after `mark_accepted()`;
- first line after leaving the rollback-capable block;
- before ownership transfer;
- during a `ReloadPreparationError` after acceptance.

For every post-acceptance injection, prove:

- candidate remains active;
- no swap rollback occurs;
- no transition rollback occurs;
- candidate abort count is zero;
- transaction is never `ABORTING` or `ABORTED`;
- finalization ownership remains registered.

### Acceptance criteria — Workstream F

- [ ] No awaited post-acceptance work remains in an aborting `try`.
- [ ] Cleanup rejects accepted transactions.
- [ ] All typed handlers branch on `reload_accepted` first.
- [ ] Exact boundary tests cover post-acceptance typed and generic exceptions.
- [ ] Candidate and transition rollback counts remain zero after acceptance.

---

# Workstream G — Make accepted results and counters truthful

## G1. Add explicit finalization status

Expose a stable field in diagnostics and the internal reload result:

```text
finalization_status = completed | retry_pending | retirement_pending | shutdown_adopted
```

Also expose:

- next pending step;
- finalization attempt count;
- last failed step;
- last error class and message;
- old generation ID;
- whether the pending swap remains committed.

## G2. Define `ok` precisely

Preserve compatibility while removing ambiguity.

Recommended semantics:

- `ok=True` means the new configuration was accepted and is authoritative;
- `finalization_status=completed` means all lifecycle work completed;
- `ok=True` plus `retry_pending` is an accepted but degraded operational result and must include a warning/message;
- preacceptance failure remains `ok=False`.

Do not label accepted pending work as a fully completed transaction.

## G3. Correct counters

At minimum distinguish:

- `accepted_reloads`;
- `fully_finalized_reloads`;
- `accepted_finalization_failures`;
- `accepted_finalization_retries`;
- `accepted_finalization_pending` current gauge;
- `retirement_retry_count`.

Do not increment `fully_finalized_reloads` until the job reaches actual completion.

## G4. Keep event recording non-authoritative

Terminal event or observer recording failures must not change finalization ownership.

Safe recording should occur after core lifecycle facts are updated, and failure should be reflected only in observer/event diagnostics.

### Acceptance criteria — Workstream G

- [ ] Results distinguish accepted from fully finalized.
- [ ] Pending results show the exact next step.
- [ ] Counters separate acceptance and completion.
- [ ] Completed counters advance only on true completion.
- [ ] Observer/event failure cannot alter ownership or completion truth.

---

# Workstream H — Correct remaining deterministic tests

## H1. Deterministically force indeterminate commit

Replace the permissive test that accepts either `rolled_back` or `indeterminate`.

Use a controlled connection double or patch that makes these observations deterministic:

- `_commit_connection()` raises;
- `in_transaction` before rollback is `True` or explicitly unknown according to the target branch;
- rollback raises, or `in_transaction` after rollback remains `True`/unknown;
- connection invalidation must occur;
- `_conn` becomes `None`;
- diagnostics report `indeterminate` and reconnect required;
- subsequent transaction access raises `DatabaseConnectionInvalidatedError`.

Keep a separate confirmed-rollback test proving the connection remains usable.

## H2. Canonical ownership-state fallback

For mock/string candidate ownership states, normalize once:

```python
state_value = getattr(candidate_state, "value", candidate_state)
```

Compare canonical lowercase values:

```python
state_value not in {"transferred", "aborted"}
```

Add tests for enum values and lowercase string doubles.

## H3. Production transition-prefix test

Retain unit tests for `TransitionApplyResult`, but add the required full reload integration path:

- transition A applies;
- transition B fails;
- transition C never runs;
- SQLite rolls back;
- staged swap rolls back;
- A rolls back exactly once;
- candidate resources close exactly once;
- old generation remains active;
- subsequent reload succeeds.

This closes the remaining gap between primitive tests and the production owner.

### Acceptance criteria — Workstream H

- [ ] Indeterminate commit branch is deterministic.
- [ ] Confirmed rollback and indeterminate tests are separate.
- [ ] Lowercase string ownership states are handled correctly.
- [ ] A/B/C transition failure runs through the full production reload path.

---

# Workstream I — CI and exact-head evidence

## I1. Update the focused CI partition

Update the Plan 018 job or add a narrowly named Plan 019 job on Python 3.11 and 3.12.

It must include:

- accepted-finalization state-machine unit tests;
- transition finalization retry tests;
- retirement fail-once and persistent-failure tests;
- active-registry pruning and retention tests;
- shutdown drain/adoption tests;
- exact acceptance-boundary tests;
- deterministic database invalidation tests;
- production transition-prefix rollback test;
- gate/lease regression tests from Plan 017/018;
- skip/xfail audit.

## I2. Required local commands

Run:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest
uv run python scripts/audit_xfail_skips.py
```

Run the focused lifecycle suite at least three times:

```bash
for i in 1 2 3; do
  uv run pytest <plan-019-focused-files> -q --tb=short
done
```

Run the retention test with deterministic cleanup and garbage collection enabled.

## I3. Archive exact-head evidence

Record:

- exact implementation commit SHA;
- Python versions;
- focused command and pass counts;
- full-suite pass count;
- skip/xfail audit result;
- 100-reload retention result;
- final active-job and history counts;
- exact old-generation retirement close counts;
- shutdown drain/adoption results;
- deterministic database invalidation result;
- CI workflow run IDs or URLs.

No closure claim is valid when evidence belongs to a different commit.

### Acceptance criteria — Workstream I

- [ ] Focused lifecycle CI runs on Python 3.11 and 3.12.
- [ ] All new exact-boundary and retention tests are included.
- [ ] Full required checks pass at exact head.
- [ ] Skip/xfail audit is clean.
- [ ] Exact-head evidence is archived.

---

# Ordered milestones

## Milestone 1 — Correct progress and retry semantics

Implement Workstreams A and B.

Exit gate:

- failed jobs remain pending at the exact step;
- retries execute that step;
- transition finalization remaining work blocks advancement;
- no false completion is possible.

## Milestone 2 — Bound ownership and retirement lifecycle

Implement Workstreams C and D.

Exit gate:

- completed jobs release operational references;
- active registry returns to zero;
- history is bounded;
- real retirement failure retains and later retires the exact old generation.

## Milestone 3 — Close shutdown and acceptance boundaries

Implement Workstreams E and F.

Exit gate:

- shutdown drains or adopts unresolved finalization before runtime shutdown;
- accepted paths cannot enter precommit cleanup;
- exact boundary tests prove zero post-acceptance rollback/abort calls.

## Milestone 4 — Truthful diagnostics and deterministic residual tests

Implement Workstreams G and H.

Exit gate:

- accepted versus finalized state is operator-visible;
- counters are truthful;
- database invalidation is deterministic;
- production transition-prefix rollback is proven.

## Milestone 5 — Exact-head verification

Implement Workstream I.

Exit gate:

- Python 3.11/3.12 focused CI is green;
- full suite is green;
- repeated lifecycle tests are green;
- exact-head evidence is recorded.

---

# Expected file targets

Likely production targets:

- `src/eggpool/control/accepted_finalization.py`
- `src/eggpool/control/reload_manager.py`
- `src/eggpool/reload_transaction.py`
- `src/eggpool/runtime_manager.py`
- `src/eggpool/db/connection.py`
- `src/eggpool/errors.py`
- `src/eggpool/app.py`
- `.github/workflows/ci.yml`

Likely test targets:

- `tests/integration/reload/test_plan_018_accepted_finalization.py`
- `tests/integration/reload/test_plan_018_retirement_retry.py`
- `tests/integration/reload/test_plan_018_database_commit_failure.py`
- `tests/integration/reload/test_plan_018_transition_ownership.py`
- new `tests/unit/test_accepted_finalization_state_machine.py`
- new `tests/integration/reload/test_plan_019_finalization_retry.py`
- new `tests/integration/reload/test_plan_019_finalization_retention.py`
- new `tests/integration/reload/test_plan_019_shutdown_drain.py`
- new `tests/integration/reload/test_plan_019_acceptance_boundary.py`
- new `tests/integration/reload/test_plan_019_database_invalidation.py`

Do not modify unrelated provider, dashboard, transcoder, router, or request-handler modules.

---

# Implementation review checklist

## Job state

- [ ] Progress and health are separate.
- [ ] Only `COMPLETED` is complete.
- [ ] Failed step remains current.
- [ ] Retry executes failed step.
- [ ] Run is single-flight.

## Transition finalization

- [ ] Production checks `remaining`.
- [ ] Pending transition finalization blocks later steps.
- [ ] Retry invokes only remaining transitions.
- [ ] Transaction fact flips only when complete.

## Registry and memory

- [ ] Active registry contains unresolved jobs only.
- [ ] Completed jobs are removed.
- [ ] Operational references are cleared.
- [ ] History is lightweight and bounded.
- [ ] Repeated reloads do not retain old graphs.

## Retirement

- [ ] Fault seam reaches production retirement.
- [ ] Committed swap remains owner on failure.
- [ ] Retry schedules exact old generation.
- [ ] Close count is exactly once.
- [ ] New reload waits for resolution.

## Shutdown

- [ ] Control server stops first.
- [ ] Active transaction is awaited.
- [ ] Finalization jobs drain before runtime shutdown.
- [ ] Persistent failure is adopted by shutdown cleanup.
- [ ] Old and active generations close exactly once.

## Acceptance boundary

- [ ] Accepted work is outside aborting `try` blocks.
- [ ] Cleanup rejects accepted transactions.
- [ ] All typed handlers check acceptance first.
- [ ] Post-acceptance faults never roll back or abort.

## Diagnostics

- [ ] Accepted and fully finalized are distinct.
- [ ] Pending step and error are visible.
- [ ] Counters distinguish acceptance, completion, and retry.
- [ ] Observer failure is non-authoritative.

## Verification

- [ ] Indeterminate DB test is deterministic.
- [ ] Ownership string fallback uses lowercase canonical values.
- [ ] Production A/B/C transition test exists.
- [ ] Retention and shutdown tests exist.
- [ ] Python 3.11/3.12 CI passes at exact head.

---

# Global closure gate

Plan 019 is complete only when all statements below are true:

1. A failed finalization attempt remains pending at the exact failed step.
2. Retrying a failed job executes that step and cannot skip directly to completion.
3. `is_complete` is true only after every required step succeeds.
4. Transition finalization failures prevent observer and retirement advancement.
5. A transition finalization retry invokes only remaining transitions.
6. A retirement failure retains the committed pending swap and exact old generation.
7. A retry schedules and closes that exact old generation exactly once.
8. A new reload cannot bypass unresolved accepted finalization.
9. Completed jobs are removed from the active registry.
10. Completed diagnostic history is bounded and contains no runtime object references.
11. Repeated successful reloads do not monotonically retain candidate, swap, transaction, or old-generation graphs.
12. Application shutdown drains accepted finalization before runtime shutdown.
13. Persistent shutdown-time finalization failure transfers old-slot ownership to deterministic shutdown cleanup.
14. Old and active generations close exactly once during shutdown.
15. No accepted transaction can enter precommit cleanup or abort state.
16. Every typed exception handler checks `txn.reload_accepted` before abort logic.
17. Accepted-but-pending results are distinguishable from fully finalized results.
18. Completion counters advance only on actual completion.
19. Retirement failure tests inject at the real production boundary.
20. Indeterminate database invalidation is deterministically proven.
21. The full production A/B/C transition-prefix rollback path is proven.
22. Focused Python 3.11 and 3.12 CI passes at the exact implementation head.
23. Full repository checks and skip/xfail audit pass at the exact implementation head.

Until every closure statement is evidenced, the accepted-finalization lifecycle remains open.