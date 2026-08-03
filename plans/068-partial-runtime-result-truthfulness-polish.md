# Plan 068 — Partial Runtime Result Truthfulness Polish

Date: 2026-08-03
Status: ready for implementation
Parent roadmap: `plans/058-durable-convergence-exact-update-sbc-hotpath-roadmap.md`
Corrective predecessor: `plans/067-explicit-handoff-and-already-terminal-runtime-closure.md`
Planning baseline: `9e53ecd125f69cf822cd9376662279c1ad7a5036`

## Purpose

Close the final reporting and verification gap left after Plan 067 without changing the terminal ownership, durable convergence, runtime lease, retry supervisor, response-handoff, or runtime-metrics architecture.

Plan 067 correctly fixed the operational defects:

- response handoff is represented by `FinalizationData.downstream_started`, not payload byte count;
- lease-owned usage, health, and account-runtime obligations no longer depend on `DurableFinalizationResult.request_transitioned`;
- an already-terminal durable request can still converge outstanding process-local runtime work;
- component markers prevent completed work from being replayed on retry.

The remaining defect is narrower. `RequestFinalizationJob._execute_runtime_release()` projects lease component markers into `FinalizationResult` only after `apply_runtime_convergence()` returns successfully. If several components complete and a later component raises, the lease is truthful but the structured result remains stale until the retry fully completes.

This polish pass must keep `FinalizationResult` synchronized with the lease on both success and failure paths and replace the weak mocked retry regression with a real middle-component failure that proves partial progress, truthful reporting, and non-replay.

## Confirmed residual defect

A representative sequence is:

1. router active count is decremented;
2. live quota reservation is removed;
3. usage is recorded;
4. health convergence raises;
5. the retained job remains at `RUNTIME_RELEASE_PENDING`.

The lease correctly records the completed `active_count`, `quota_reservation`, and `usage` markers. However, the exception path currently updates only:

- `runtime_cleanup_complete=False`;
- `retryable=True`;
- `detail="runtime cleanup incomplete"`.

It does not project already-completed markers into fields such as `active_count_decremented`, `quota_reservation_removed`, or `health_released_or_recorded`. Operators and tests therefore see a stale result during retry-pending convergence even though the lease itself would prevent replay.

The current retry regression also uses a fake `apply_runtime_convergence()` that raises before representing real partial component progress. It proves that durable finalization is not rerun, but it does not prove that completed runtime components remain reported and are not replayed after a middle-component failure.

## Scope

Primary runtime file:

- `src/eggpool/request/finalization_job.py`

Focused test files:

- `tests/unit/test_request_finalization_state_machine.py`
- `tests/unit/test_request_finalizer.py`

Planning metadata after implementation:

- `plans/058-durable-convergence-exact-update-sbc-hotpath-roadmap.md`
- `plans/066-terminal-runtime-ownership-and-supervisor-closure.md`
- `plans/067-explicit-handoff-and-already-terminal-runtime-closure.md`
- this plan

Only touch `src/eggpool/request/finalizer.py` if a minimal test seam is genuinely required. Do not change runtime convergence ordering or ownership semantics merely to simplify a test.

## Explicitly out of scope

- changing `FinalizationData.downstream_started` or stream call sites;
- changing durable request, attempt, reservation, or cost transactions;
- adding another runtime lease or result hierarchy;
- adding a generic event/effects graph;
- persisting component markers in SQLite;
- changing supervisor retry limits, backoff, capacity, or scheduling;
- adding a queue, worker, migration, dependency, metrics service, or endpoint;
- redesigning health, quota, router, account registry, or stale repair;
- adding result fields unrelated to an existing caller or documented semantic requirement;
- adding new CI jobs, matrices, coverage gates, fault campaigns, soak tests, benchmark gates, or evidence bundles;
- creating plan-numbered test modules.

## Governing decisions

1. `AttemptRuntimeLease.completed_components` remains the source of truth for process-local component progress.
2. `FinalizationResult` is a projection of durable facts plus the current lease state; it must not invent a second ownership record.
3. Result projection must occur after every runtime-convergence attempt, including attempts that raise after partial success.
4. A failed runtime attempt remains retryable and keeps job progress at `RUNTIME_RELEASE_PENDING`.
5. Completed lease components remain marked and are skipped by the next retry.
6. Durable finalization remains cached in `_durable_result` and is not rerun for a runtime-only retry.
7. Verification must exercise the real component loop with deterministic fakes, not a fake method that raises before recording progress.
8. Keep the patch small enough for direct review. A private helper is preferable to duplicated field construction in success and exception branches.

## Phase A — Project lease progress truthfully on every attempt

### Required changes

1. Add one narrow helper on `RequestFinalizationJob`, for example `_refresh_runtime_result_from_lease()`, that derives current runtime result facts from `self.runtime_lease.completed_components` and `self.runtime_lease.released`.
2. At minimum, project the existing fields consistently:
   - `quota_reservation_removed` from the `quota_reservation` marker;
   - `active_count_decremented` from the `active_count` marker;
   - `health_released_or_recorded` from either the `health_probe` or `health` marker;
   - `runtime_cleanup_complete` from `runtime_lease.released`.
3. Preserve durable fields already stored in `_result`; use `dataclasses.replace()` or the repository’s existing equivalent rather than reconstructing and accidentally dropping durable facts.
4. Call the helper after `apply_runtime_convergence()` succeeds.
5. Also call it in the exception path before setting retry metadata and re-raising. The resulting state must retain successful component facts while keeping:
   - `runtime_cleanup_complete=False`;
   - `retryable=True`;
   - an accurate bounded detail string.
6. Ensure a lease with no acquired/required work still reports complete only when the lease convergence method has declared it released.
7. Do not infer component completion from whether a dependency exists, whether durable state transitioned, cost magnitude, or a previous `FinalizationResult` value.
8. Do not add mutable mirrors of `_released_components` to `RequestFinalizationJob`.
9. Preserve the no-lease compatibility behavior unless a production invariant already prohibits it. This pass must not reopen the broader legacy-construction cleanup.

### Acceptance criteria

- A runtime attempt that completes active-count and quota cleanup before a later failure reports both completed fields immediately.
- `runtime_cleanup_complete` remains false while any acquired or required component is incomplete.
- A later retry updates only newly completed fields and ultimately sets `runtime_cleanup_complete=True`.
- Durable terminal and reservation facts are preserved while runtime fields change.
- Result projection uses lease markers and does not become a second source of ownership truth.

## Phase B — Replace weak retry coverage with a real middle-component failure

### Required test shape

Replace or strengthen the existing mocked runtime retry test so it exercises the real `RequestFinalizer.apply_runtime_convergence()` sequence through `RequestFinalizationJob`.

Use deterministic fakes with counters:

- router decrement succeeds once;
- quota reservation removal succeeds once;
- usage recording succeeds once;
- health outcome application raises on the first attempt and succeeds on the retry;
- account-runtime update occurs once after health succeeds, following the actual component ordering;
- durable `finalize()` returns one converged result and records its invocation count.

After the first `job.run()` raises, assert:

- `job.progress == RUNTIME_RELEASE_PENDING`;
- durable finalization was invoked once;
- lease markers contain all components that completed before the injected failure;
- result fields for active count and quota removal are true;
- health completion is false unless a probe release genuinely completed;
- `runtime_cleanup_complete` is false;
- `retryable` is true.

After the retry, assert:

- durable finalization is still invoked once;
- active-count, quota-removal, and usage counters remain one;
- health is retried only as required;
- account-runtime update occurs once;
- the lease is released;
- all applicable result fields are truthful;
- `runtime_cleanup_complete` is true.

### Truthfulness regression

Add at most one separate compact assertion case if the integrated retry test would otherwise obscure the durable/runtime distinction. It should prove that:

- durable reservation convergence can be true;
- some runtime component fields can already be true;
- a later runtime component can still be incomplete;
- `runtime_cleanup_complete` remains false until actual convergence.

Prefer extending the integrated test rather than creating redundant fixtures.

### Test budget

- Modify or replace one existing retry test.
- Add no more than one additional focused result-projection test.
- Use existing test files only.
- No sleeps beyond existing task-scheduling mechanics.
- No live provider, HTTP, subprocess, or long-running fault tests.

### Acceptance criteria

- The test fails against `9e53ecd125f69cf822cd9376662279c1ad7a5036` because partial result fields remain stale.
- The corrected implementation passes while using the real runtime convergence method.
- The test proves non-replay of completed components, not merely non-replay of durable finalization.
- The test proves truthful partial and final `FinalizationResult` state.

## Phase C — Focused verification and metadata closure

### Required local checks

Run the smallest useful focused set first:

```bash
uv run ruff format src/eggpool/request/finalization_job.py tests/unit/test_request_finalization_state_machine.py tests/unit/test_request_finalizer.py
uv run ruff check src/eggpool/request/finalization_job.py tests/unit/test_request_finalization_state_machine.py tests/unit/test_request_finalizer.py
uv run pytest tests/unit/test_request_finalization_state_machine.py tests/unit/test_request_finalizer.py -q --tb=short --maxfail=1
uv run pyright src/eggpool/request/finalization_job.py src/eggpool/request/finalizer.py
```

Then run the existing repository gate:

```bash
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

Do not add CI, retained evidence files, repeated fault loops, or timing assertions. Record exact commands and outcomes in this plan’s implementation closure note or the implementation commit.

### Planning closure

After implementation and verification:

1. mark Plan 068 complete and check each satisfied criterion;
2. add a short post-review note to Plan 067 identifying Plan 068 as the result-truthfulness polish successor;
3. keep Plan 067’s operational handoff and already-terminal criteria complete;
4. mark only its “truthful throughout incomplete convergence” and corresponding focused-regression criteria complete after the new tests pass;
5. update Plan 066 similarly without erasing its historical implementation record;
6. register Plan 068 under Roadmap 058;
7. return Roadmap 058 to `completed` only after partial result projection and real middle-component retry evidence pass;
8. do not reopen unrelated dispatch, recovery, update, quota, scheduler, metrics, or handoff criteria.

## Recommended implementation sequence

1. add the lease-to-result projection helper;
2. invoke it on both success and exception paths;
3. replace the fake retry test with the real component failure sequence;
4. add one compact partial-result assertion only if still needed;
5. run focused checks and the existing smoke gate;
6. reconcile Plans 058, 066, 067, and 068.

This should be one runtime/test commit plus one optional documentation closure commit. Do not split the helper and each assertion into ceremonial commits.

## Plan acceptance criteria

- [ ] `FinalizationResult` is refreshed from lease component markers after every runtime-convergence attempt.
- [ ] Successful early components remain visible when a later runtime component raises.
- [ ] `runtime_cleanup_complete` remains false until the lease is actually released.
- [ ] Durable result fields are preserved while runtime result fields advance.
- [ ] A real middle-component failure leaves the job at `RUNTIME_RELEASE_PENDING`.
- [ ] Retry does not rerun durable finalization or completed active/quota/usage components.
- [ ] Retry completes only the outstanding runtime components and produces truthful final state.
- [ ] Focused tests prove partial and final result truthfulness.
- [ ] Plans 058, 066, 067, and 068 have coherent closure metadata.
- [ ] Focused checks and the existing smoke gate pass.
- [ ] No queue, migration, dependency, result hierarchy, lifecycle framework, CI expansion, fault matrix, soak gate, benchmark gate, or evidence system is introduced.

## Rejection conditions

Do not close this plan if:

- the exception path still leaves completed lease components unreflected in `FinalizationResult`;
- result state is derived from durable transition rather than current lease markers;
- the retry test still replaces the real component loop with a fake method that raises before partial progress;
- completed active, quota, usage, health, probe, or account-runtime work can be replayed;
- `runtime_cleanup_complete` can become true while required lease markers are incomplete;
- durable result fields are lost while refreshing runtime fields;
- implementation introduces a second ownership record or disproportionate test infrastructure;
- planning documents claim full closure before focused checks pass.

## Definition of done

This polish pass is complete when a retry-pending finalization result truthfully exposes every runtime component that has already converged, remains incomplete for outstanding work, resumes the real component loop without replay, becomes complete after the remaining components succeed, passes the two focused regression shapes and existing smoke gate, and closes Plans 058, 066, 067, and 068 without adding infrastructure.